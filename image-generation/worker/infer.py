"""Runs only in the disposable worker. Parent suppresses third-party text output."""

import errno
import json
import sys
import time
from pathlib import Path

from resources import GIB

from ear_image.contract import ModelSpec, WorkerInput

# Explicit component allowlist excludes .bin, fp16 duplicates, single-file weights and Python code.
FILES = {
    "model_index.json",
    "scheduler/scheduler_config.json",
    "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors",
    "unet/config.json",
    "unet/diffusion_pytorch_model.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
}


def run(folder, cache, threads):
    import torch
    from diffusers import StableDiffusionPipeline
    from huggingface_hub import HfApi, snapshot_download

    task = WorkerInput.model_validate_json((folder / "input.json").read_bytes())
    spec = ModelSpec.model_validate_json((folder / "model.json").read_bytes())
    if torch.version.cuda is not None:
        raise RuntimeError("CPU wheel required.")
    torch.set_num_threads(threads)
    timings = {}
    start = time.monotonic()
    api = HfApi()
    files = api.model_info(spec.model_id, revision=spec.model_revision, files_metadata=True)
    sizes = {f.rfilename: f.size for f in files.siblings if f.rfilename in FILES}
    if (
        files.sha != spec.model_revision
        or sizes.keys() != FILES
        or any(v is None for v in sizes.values())
    ):
        raise ValueError("Unsupported layout")
    if sum(sizes.values()) > 6 * GIB:
        return "insufficient_resources"
    snapshot_download(
        spec.model_id,
        revision=spec.model_revision,
        local_dir=cache,
        allow_patterns=sorted(FILES),
        max_workers=2,
    )
    timings["model_download"] = time.monotonic() - start
    index = json.loads((cache / "model_index.json").read_text())
    if index["_class_name"] != "StableDiffusionPipeline" or any(
        isinstance(v, list) and v[0] not in (None, "diffusers", "transformers") for v in index.values()
    ):
        raise ValueError("Unsupported pipeline")
    start = time.monotonic()
    pipe = StableDiffusionPipeline.from_pretrained(
        str(cache),
        torch_dtype=torch.float32,
        use_safetensors=True,
        local_files_only=True,
        low_cpu_mem_usage=True,
    ).to("cpu")
    pipe.set_progress_bar_config(disable=True)
    timings["model_load"] = time.monotonic() - start
    (folder / "stage").write_text("inference_failed")
    start = time.monotonic()
    with torch.inference_mode():
        image = pipe(
            prompt=task.prompt,
            width=512,
            height=512,
            num_images_per_prompt=1,
            num_inference_steps=1,
            guidance_scale=0.0,
            generator=torch.Generator(device="cpu").manual_seed(task.seed),
        ).images[0]
    timings["inference"] = time.monotonic() - start
    (folder / "stage").write_text("invalid_png")
    image.convert("RGB").save(folder / "output.png", format="PNG")
    (folder / "timings.json").write_text(json.dumps(timings))


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    try:
        code = run(folder, Path(sys.argv[2]), int(sys.argv[3]))
    except Exception as exc:
        memory_error = isinstance(exc, MemoryError) or getattr(exc, "errno", None) in (
            errno.ENOMEM,
            errno.ENOSPC,
        )
        alloc_error = any(
            x in str(exc).lower()
            for x in (
                "can't allocate memory",
                "cannot allocate memory",
                "out of memory",
                "no space left on device",
            )
        )
        code = (
            "insufficient_resources" if memory_error or alloc_error else (folder / "stage").read_text()
        )
    if code:
        (folder / "stage").write_text(code)
        sys.exit(1)
