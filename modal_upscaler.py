import io
import os
import base64
import modal
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

MODEL_URL = "https://huggingface.co/FelipeMurguia/4xNomos8kSCHAT-L/resolve/main/4xNomos8kSCHAT-L.pth"
MODEL_PATH = "/models/4xNomos8kSCHAT-L.pth"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "libgl1", "libglib2.0-0")
    .pip_install(
        "torch",
        "torchvision",
        "spandrel",
        "einops",
        "safetensors",
        "pillow",
        "numpy",
        "opencv-python-headless",
        "fastapi[standard]",
    )
    .run_commands(
        f"mkdir -p /models && curl -L --fail -o {MODEL_PATH} {MODEL_URL}"
    )
)

app = modal.App("zephyr-a1-upscaler")


@app.cls(
    gpu="L4",
    image=image,
    scaledown_window=60,
    timeout=180,
    secrets=[modal.Secret.from_name("zephyr-upscale-secret")],
)
class ZephyrA1Upscaler:
    @modal.enter()
    def load_model(self):
        import torch
        from spandrel import ModelLoader

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        descriptor = ModelLoader().load_from_file(MODEL_PATH)
        self.model = descriptor.model.to(self.device).eval()
        self.scale = descriptor.scale  # 4x

    def _tile_upscale(self, img_np, tile_size: int = 256, overlap: int = 32):
        import numpy as np
        import torch
        import torch.nn.functional as F

        h, w, _ = img_np.shape
        scale = self.scale
        out_h, out_w = h * scale, w * scale

        output_acc = np.zeros((out_h, out_w, 3), dtype=np.float32)
        weight_acc = np.zeros((out_h, out_w, 1), dtype=np.float32)

        step = tile_size - overlap
        ys = list(range(0, max(h - tile_size, 0) + 1, step))
        xs = list(range(0, max(w - tile_size, 0) + 1, step))
        if not ys or ys[-1] + tile_size < h:
            ys.append(max(h - tile_size, 0))
        if not xs or xs[-1] + tile_size < w:
            xs.append(max(w - tile_size, 0))

        ys = sorted(set(ys))
        xs = sorted(set(xs))

        with torch.no_grad():
            for y0 in ys:
                for x0 in xs:
                    y1 = min(y0 + tile_size, h)
                    x1 = min(x0 + tile_size, w)
                    tile = img_np[y0:y1, x0:x1]
                    th, tw, _ = tile.shape

                    # Pad to multiple of 16 required by HAT-L window attention
                    pad_h = (16 - (th % 16)) % 16
                    pad_w = (16 - (tw % 16)) % 16

                    t = torch.from_numpy(tile).permute(2, 0, 1).unsqueeze(0).to(self.device, dtype=torch.float32)
                    if pad_h > 0 or pad_w > 0:
                        t = F.pad(t, (0, pad_w, 0, pad_h), mode="reflect")

                    with torch.amp.autocast("cuda", dtype=torch.float16):
                        pred = self.model(t)

                    pred = pred[:, :, : th * scale, : tw * scale].float().clamp(0.0, 1.0)
                    pred_np = pred.squeeze(0).permute(1, 2, 0).cpu().numpy()

                    # Smooth feather mask so tile borders are completely invisible
                    ph, pw, _ = pred_np.shape
                    ov_px = overlap * scale
                    wy = np.ones((ph, 1), dtype=np.float32)
                    wx = np.ones((1, pw), dtype=np.float32)

                    if y0 > 0 and ov_px < ph:
                        wy[:ov_px, 0] = np.linspace(0.05, 1.0, ov_px)
                    if y1 < h and ov_px < ph:
                        wy[-ov_px:, 0] = np.linspace(1.0, 0.05, ov_px)
                    if x0 > 0 and ov_px < pw:
                        wx[0, :ov_px] = np.linspace(0.05, 1.0, ov_px)
                    if x1 < w and ov_px < pw:
                        wx[0, -ov_px:] = np.linspace(1.0, 0.05, ov_px)

                    mask = (wy * wx)[:, :, None]
                    oy0, oy1 = y0 * scale, y1 * scale
                    ox0, ox1 = x0 * scale, x1 * scale

                    output_acc[oy0:oy1, ox0:ox1] += pred_np * mask
                    weight_acc[oy0:oy1, ox0:ox1] += mask

        return np.clip(output_acc / np.maximum(weight_acc, 1e-6), 0.0, 1.0)

    def _studio_luminance_finish(self, rgb_u8):
        import cv2
        import numpy as np

        # Work strictly in CIE LAB Luminance channel so skin tones & colors stay 100% true
        lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB)
        l_chan, a_chan, b_chan = cv2.split(lab)

        # 1. Gentle local micro-contrast (DSLR depth without crushing shadows)
        clahe = cv2.createCLAHE(clipLimit=1.25, tileGridSize=(8, 8))
        l_clahe = clahe.apply(l_chan)
        l_blend = cv2.addWeighted(l_chan, 0.72, l_clahe, 0.28, 0)

        # 2. Dual-radius luminance-only optical sharpening (zero chromatic color halos on text)
        blur_fine = cv2.GaussianBlur(l_blend, (0, 0), sigmaX=0.8)
        sharp_fine = cv2.addWeighted(l_blend, 1.38, blur_fine, -0.38, 0)

        blur_med = cv2.GaussianBlur(sharp_fine, (0, 0), sigmaX=2.2)
        l_final = cv2.addWeighted(sharp_fine, 1.15, blur_med, -0.15, 0)

        merged = cv2.merge([np.clip(l_final, 0, 255).astype(np.uint8), a_chan, b_chan])
        return cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)

    @modal.fastapi_endpoint(method="POST")
    async def upscale(self, request: Request):
        import numpy as np
        from PIL import Image

        # Verify shared secret so outsiders cannot use your free GPU credits
        expected_key = os.environ.get("ZEPHYR_UPSCALE_KEY", "")
        if expected_key and request.headers.get("x-zephyr-key") != expected_key:
            raise HTTPException(status_code=401, detail="Unauthorized")

        body = await request.json()
        img_b64 = body.get("image_b64")
        if not img_b64:
            raise HTTPException(status_code=400, detail="Missing image_b64")

        raw_bytes = base64.b64decode(img_b64)
        orig_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        orig_w, orig_h = orig_img.size

        # Cap input max edge at 1400px before 4x HAT-L (yields up to 5600px true neural master)
        max_input_side = 1400
        if max(orig_w, orig_h) > max_input_side:
            ratio = max_input_side / max(orig_w, orig_h)
            orig_img = orig_img.resize(
                (int(orig_w * ratio), int(orig_h * ratio)),
                Image.Resampling.LANCZOS,
            )

        img_np = np.asarray(orig_img, dtype=np.float32) / 255.0
        upscaled_np = self._tile_upscale(img_np, tile_size=256, overlap=32)
        upscaled_u8 = (upscaled_np * 255.0).round().astype(np.uint8)

        # Apply studio optical finish on luminance channel
        finished_u8 = self._studio_luminance_finish(upscaled_u8)
        final_img = Image.fromarray(finished_u8, mode="RGB")

        # 4:4:4 zero chroma-subsampling JPEG for razor-sharp text & minimal payload
        out_buf = io.BytesIO()
        final_img.save(out_buf, format="JPEG", quality=97, subsampling=0, optimize=True)
        out_b64 = base64.b64encode(out_buf.getvalue()).decode("utf-8")

        return JSONResponse({
            "upscaled_url": f"data:image/jpeg;base64,{out_b64}",
            "engine": "Zephyr HAT-L 8K Transformer GPU",
            "width": final_img.width,
            "height": final_img.height,
        })