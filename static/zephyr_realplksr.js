(function () {
    const ORT_CDN = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/";
    const DIRECT_HF_URL = "https://huggingface.co/darktable-org/upscale-realplksr-onnx/resolve/main/onnx/model_x4.onnx";
    const PROXY_MODEL_URL = "/api/ai-model/realplksr-x4.onnx";
    const CACHE_NAME = "zephyr-realplksr-cache-v1";

    const TILE_SIZE = 256;   // Required static input size for RealPLKSR x4
    const OVERLAP = 16;      // Overlap border trimmed per side to eliminate tile seams
    const STEP = TILE_SIZE - 2 * OVERLAP; // 224 px core per tile
    const SCALE = 4;         // 256x256 -> 1024x1024

    let ortLoadPromise = null;
    let sessionPromise = null;
    let activeProvider = "WebGPU";

    function loadOrtScript() {
        if (window.ort) return Promise.resolve(window.ort);
        if (ortLoadPromise) return ortLoadPromise;
        ortLoadPromise = new Promise((resolve, reject) => {
            const s = document.createElement("script");
            s.src = ORT_CDN + "ort.webgpu.min.js";
            s.onload = () => resolve(window.ort);
            s.onerror = () => reject(new Error("Failed to load ONNX Runtime Web"));
            document.head.appendChild(s);
        });
        return ortLoadPromise;
    }

    async function fetchModelBuffer(onStatus) {
        if ("caches" in window) {
            try {
                const cache = await caches.open(CACHE_NAME);
                const cached = await cache.match(PROXY_MODEL_URL);
                if (cached) {
                    if (onStatus) onStatus("Loading cached RealPLKSR neural weights...");
                    return await cached.arrayBuffer();
                }
            } catch (_) {}
        }

        const urls = [DIRECT_HF_URL, PROXY_MODEL_URL];
        let lastErr = null;
        for (const url of urls) {
            try {
                if (onStatus) onStatus("Downloading RealPLKSR 4x model (29.7 MB, cached once)...");
                const resp = await fetch(url);
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                const buf = await resp.arrayBuffer();
                if (buf.byteLength < 25_000_000) throw new Error("Incomplete model file");

                if ("caches" in window) {
                    try {
                        const cache = await caches.open(CACHE_NAME);
                        await cache.put(
                            PROXY_MODEL_URL,
                            new Response(buf.slice(0), {
                                headers: { "Content-Type": "application/octet-stream" },
                            })
                        );
                    } catch (_) {}
                }
                return buf;
            } catch (err) {
                lastErr = err;
            }
        }
        throw lastErr || new Error("Failed to download RealPLKSR model");
    }

    async function getSession(onStatus) {
        if (sessionPromise) return sessionPromise;
        sessionPromise = (async () => {
            if (onStatus) onStatus("Initializing Zephyr RealPLKSR Engine...");
            const ort = await loadOrtScript();
            ort.env.wasm.wasmPaths = ORT_CDN;
            ort.env.wasm.numThreads = self.crossOriginIsolated
                ? Math.min(navigator.hardwareConcurrency || 4, 4)
                : 1;

            const modelBuffer = await fetchModelBuffer(onStatus);

            if (navigator.gpu) {
                try {
                    if (onStatus) onStatus("Compiling RealPLKSR for your GPU (WebGPU)...");
                    activeProvider = "WebGPU GPU";
                    return await ort.InferenceSession.create(modelBuffer, {
                        executionProviders: ["webgpu"],
                        graphOptimizationLevel: "all",
                    });
                } catch (gpuErr) {
                    console.warn("WebGPU unavailable, falling back to WASM:", gpuErr);
                }
            }

            if (onStatus) onStatus("Loading RealPLKSR on Neural CPU (WASM)...");
            activeProvider = "WASM Neural CPU";
            return await ort.InferenceSession.create(modelBuffer, {
                executionProviders: ["wasm"],
                graphOptimizationLevel: "all",
            });
        })().catch((err) => {
            sessionPromise = null;
            throw err;
        });
        return sessionPromise;
    }

    function fileToImage(source) {
        return new Promise((resolve, reject) => {
            if (source instanceof HTMLImageElement && source.complete && source.naturalWidth) {
                return resolve({ img: source, revokeUrl: null });
            }
            const img = new Image();
            img.crossOrigin = "anonymous";
            const objUrl = source instanceof Blob ? URL.createObjectURL(source) : null;
            img.onload = () => resolve({ img, revokeUrl: objUrl });
            img.onerror = (err) => {
                if (objUrl) URL.revokeObjectURL(objUrl);
                reject(err);
            };
            img.src = objUrl || source;
        });
    }

    function reflectIndex(i, n) {
        if (n <= 1) return 0;
        while (i < 0 || i >= n) {
            if (i < 0) i = -i;
            if (i >= n) i = 2 * n - 2 - i;
        }
        return i;
    }

    function applyLuminanceClarity(imageData, amount) {
        const { data, width, height } = imageData;
        const lum = new Float32Array(width * height);
        for (let i = 0, p = 0; i < data.length; i += 4, p++) {
            lum[p] = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        }
        for (let y = 1; y < height - 1; y++) {
            const row = y * width;
            for (let x = 1; x < width - 1; x++) {
                const idx = row + x;
                const avg = (
                    lum[idx - width - 1] + lum[idx - width] + lum[idx - width + 1] +
                    lum[idx - 1]         + lum[idx]         + lum[idx + 1] +
                    lum[idx + width - 1] + lum[idx + width] + lum[idx + width + 1]
                ) / 9;
                const delta = (lum[idx] - avg) * amount;
                const p = idx * 4;
                data[p]     = Math.max(0, Math.min(255, data[p]     + delta));
                data[p + 1] = Math.max(0, Math.min(255, data[p + 1] + delta));
                data[p + 2] = Math.max(0, Math.min(255, data[p + 2] + delta));
            }
        }
    }

    window.zephyrNeuralUpscale = async function (fileOrImg, onStatus) {
        const session = await getSession(onStatus);
        const ort = window.ort;
        const { img, revokeUrl } = await fileToImage(fileOrImg);

        try {
            const maxSide = activeProvider === "WebGPU GPU" ? 1024 : 672;
            let inW = img.naturalWidth || img.width;
            let inH = img.naturalHeight || img.height;
            if (Math.max(inW, inH) > maxSide) {
                const ratio = maxSide / Math.max(inW, inH);
                inW = Math.max(16, Math.round(inW * ratio));
                inH = Math.max(16, Math.round(inH * ratio));
            }

            const inCanvas = document.createElement("canvas");
            inCanvas.width = inW;
            inCanvas.height = inH;
            const inCtx = inCanvas.getContext("2d", { willReadFrequently: true });
            inCtx.imageSmoothingEnabled = true;
            inCtx.imageSmoothingQuality = "high";
            inCtx.drawImage(img, 0, 0, inW, inH);
            const srcData = inCtx.getImageData(0, 0, inW, inH).data;

            const outW = inW * SCALE;
            const outH = inH * SCALE;
            const outCanvas = document.createElement("canvas");
            outCanvas.width = outW;
            outCanvas.height = outH;
            const outCtx = outCanvas.getContext("2d");
            const outImgData = outCtx.createImageData(outW, outH);
            const dst = outImgData.data;

            const tilesY = Math.ceil(inH / STEP);
            const tilesX = Math.ceil(inW / STEP);
            const totalTiles = tilesY * tilesX;
            let doneTiles = 0;

            const inputName = session.inputNames[0];
            const outputName = session.outputNames[0];
            const tileArea = TILE_SIZE * TILE_SIZE;
            const outTileDim = TILE_SIZE * SCALE;
            const outTileArea = outTileDim * outTileDim;
            const ovOut = OVERLAP * SCALE;

            for (let tyIdx = 0; tyIdx < tilesY; tyIdx++) {
                const coreY = tyIdx * STEP;
                const coreH = Math.min(STEP, inH - coreY);

                for (let txIdx = 0; txIdx < tilesX; txIdx++) {
                    const coreX = txIdx * STEP;
                    const coreW = Math.min(STEP, inW - coreX);

                    const inputArr = new Float32Array(3 * tileArea);
                    let idx = 0;
                    for (let py = 0; py < TILE_SIZE; py++) {
                        const sy = reflectIndex(coreY + py - OVERLAP, inH);
                        const rowOffset = sy * inW;
                        for (let px = 0; px < TILE_SIZE; px++) {
                            const sx = reflectIndex(coreX + px - OVERLAP, inW);
                            const sp = (rowOffset + sx) * 4;
                            inputArr[idx] = srcData[sp] / 255.0;
                            inputArr[tileArea + idx] = srcData[sp + 1] / 255.0;
                            inputArr[2 * tileArea + idx] = srcData[sp + 2] / 255.0;
                            idx++;
                        }
                    }

                    const tensor = new ort.Tensor("float32", inputArr, [1, 3, TILE_SIZE, TILE_SIZE]);
                    const results = await session.run({ [inputName]: tensor });
                    const outTensor = results[outputName];
                    const outData = outTensor.data;

                    const writeH = coreH * SCALE;
                    const writeW = coreW * SCALE;
                    const dstStartY = coreY * SCALE;
                    const dstStartX = coreX * SCALE;

                    for (let oy = 0; oy < writeH; oy++) {
                        const srcRow = (ovOut + oy) * outTileDim + ovOut;
                        const dstRow = (dstStartY + oy) * outW + dstStartX;
                        for (let ox = 0; ox < writeW; ox++) {
                            const ti = srcRow + ox;
                            const dp = (dstRow + ox) * 4;
                            dst[dp]     = Math.round(Math.max(0, Math.min(1, outData[ti])) * 255);
                            dst[dp + 1] = Math.round(Math.max(0, Math.min(1, outData[outTileArea + ti])) * 255);
                            dst[dp + 2] = Math.round(Math.max(0, Math.min(1, outData[2 * outTileArea + ti])) * 255);
                            dst[dp + 3] = 255;
                        }
                    }

                    if (tensor.dispose) tensor.dispose();
                    if (outTensor.dispose) outTensor.dispose();

                    doneTiles++;
                    if (onStatus) {
                        const pct = Math.round((doneTiles / totalTiles) * 100);
                        onStatus(`RealPLKSR ${activeProvider}: ${pct}%`);
                    }
                    await new Promise((r) => setTimeout(r, 0));
                }
            }

            applyLuminanceClarity(outImgData, 0.22);
            outCtx.putImageData(outImgData, 0, 0);

            return {
                upscaled_url: outCanvas.toDataURL("image/jpeg", 0.96),
                engine: `Zephyr RealPLKSR 4x (${activeProvider})`,
                width: outW,
                height: outH,
            };
        } finally {
            if (revokeUrl) URL.revokeObjectURL(revokeUrl);
        }
    };
})();