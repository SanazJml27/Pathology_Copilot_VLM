from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pathlib import Path
import subprocess
import uuid
import json
import shutil
import os
import time

app = FastAPI(title="PA-LLaVA ROI Inference Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PALAVA_ROOT = Path(
    os.environ.get(
        "PALAVA_ROOT",
        "/Users/sanazjamalzadeh/Documents/pathology-copilot-mvp/external/PA-LLaVA-clean",
    )
).resolve()

LLM_MODEL = os.environ.get(
    "PALAVA_LLM_MODEL",
    "meta-llama/Meta-Llama-3-8B-Instruct",
)

LLAVA_PATH = os.environ.get(
    "PALAVA_LLAVA_PATH",
    "weights/instruction_tuning_weight_ft",
)

PROMPT_TEMPLATE = os.environ.get(
    "PALAVA_PROMPT_TEMPLATE",
    "llama3_chat",
)


def find_latest_result(work_dir: Path) -> Path | None:
    result_files = list(work_dir.rglob("zeroshot_result.json"))
    if not result_files:
        return None
    return max(result_files, key=lambda p: p.stat().st_mtime)


def extract_answer(obj):
    """
    Robust parser because PA-LLaVA result JSON formats can vary.
    Returns a readable answer plus the raw result.
    """
    if isinstance(obj, str):
        return obj

    if isinstance(obj, list):
        for item in obj:
            answer = extract_answer(item)
            if answer:
                return answer
        return None

    if isinstance(obj, dict):
        preferred_keys = [
            "prediction",
            "pred",
            "answer",
            "response",
            "output",
            "text",
            "caption",
            "result",
        ]

        for key in preferred_keys:
            if key in obj and obj[key]:
                if isinstance(obj[key], str):
                    return obj[key]
                nested = extract_answer(obj[key])
                if nested:
                    return nested

        for value in obj.values():
            nested = extract_answer(value)
            if nested:
                return nested

    return None


@app.get("/health")
def health():
    return {
        "status": "ok",
        "palava_root": str(PALAVA_ROOT),
        "llava_path": LLAVA_PATH,
    }


@app.post("/predict")
async def predict(
    image: UploadFile = File(...),
    question: str = Form("Describe the key histopathological features in this pathology image."),
):
    if not PALAVA_ROOT.exists():
        raise HTTPException(status_code=500, detail=f"PA-LLaVA root not found: {PALAVA_ROOT}")

    request_id = uuid.uuid4().hex
    run_root = PALAVA_ROOT / "service_runs" / request_id
    input_dir = run_root / "input"
    work_dir = run_root / "work"

    input_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    image_path = input_dir / "roi.jpg"
    data_path = input_dir / "request.json"
    log_path = run_root / "run.log"

    with image_path.open("wb") as f:
        shutil.copyfileobj(image.file, f)

    # Single user-question mode.
    # Send exactly the pathologist/user question to PA-LLaVA.
    # Add only a light instruction to avoid one-word answers.
    effective_question = question.strip() or (
        "Could you provide a detailed description of what is shown in this pathology image?"
    )

    data = {
        "sample_001": {
            "image": str(image_path.resolve()),
            "question": effective_question,
            "answer": "",
        }
    }
    data_path.write_text(json.dumps(data, indent=2))

    cmd = [
        "xtuner",
        "zero_shot",
        LLM_MODEL,
        "--visual-encoder",
        "PLIP",
        "--llava",
        LLAVA_PATH,
        "--prompt-template",
        PROMPT_TEMPLATE,
        "--data-path",
        str(data_path),
        "--work-dir",
        str(work_dir),
        "--launcher",
        "none",
        "--anyres-image",
    ]

    started = time.time()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PALAVA_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="PA-LLaVA inference timed out.")

    elapsed = round(time.time() - started, 2)
    log_path.write_text(proc.stdout)

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "PA-LLaVA command failed.",
                "returncode": proc.returncode,
                "log_path": str(log_path),
                "tail": proc.stdout[-4000:],
            },
        )

    result_path = find_latest_result(work_dir)

    if result_path is None:
        return {
            "answer": None,
            "message": "PA-LLaVA finished but no zeroshot_result.json was found.",
            "elapsed_seconds": elapsed,
            "run_dir": str(run_root),
            "log_path": str(log_path),
            "stdout_tail": proc.stdout[-4000:],
        }

    raw_result = json.loads(result_path.read_text())

    answer = None

    # PA-LLaVA zero_shot usually returns:
    # [{"question_id": "sample_001", "answer": "..."}]
    if isinstance(raw_result, list) and raw_result:
        first = raw_result[0]
        if isinstance(first, dict):
            answer = (
                first.get("answer")
                or first.get("prediction")
                or first.get("pred")
                or first.get("response")
            )

    if not answer:
        answer = extract_answer(raw_result)

    return {
        "answer": answer,
        "raw_result": raw_result,
        "elapsed_seconds": elapsed,
        "result_path": str(result_path),
        "run_dir": str(run_root),
    }


@app.get("/", response_class=HTMLResponse)
def upload_page():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>PA-LLaVA Pathology Upload</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
    textarea { width: 100%; height: 80px; font-size: 15px; }
    button { padding: 10px 18px; font-size: 16px; cursor: pointer; }
    img { max-width: 550px; max-height: 420px; display: block; margin-top: 15px; border: 1px solid #ddd; }
    pre { white-space: pre-wrap; background: #f6f6f6; padding: 16px; border-radius: 8px; }
    .answer { font-size: 20px; font-weight: 600; margin-top: 20px; padding: 14px; background: #eef7ee; border-radius: 8px; }
  </style>
</head>
<body>
  <h1>PA-LLaVA Pathology Upload</h1>
  <p>Upload a pathology image or ROI crop and ask a question. Research/demo use only.</p>

  <form id="form">
    <p>
      <b>Image</b><br>
      <input type="file" id="image" accept="image/*" required>
    </p>

    <p>
      <b>Question</b><br>
      <textarea id="question">What is the most likely diagnosis for this pathology image?</textarea>
    </p>

    <button type="submit">Run PA-LLaVA</button>
  </form>

  <img id="preview" style="display:none;" />

  <div id="status"></div>
  <div id="answer" class="answer" style="display:none;"></div>

  <h3>Raw response</h3>
  <pre id="raw"></pre>

<script>
const form = document.getElementById("form");
const imageInput = document.getElementById("image");
const preview = document.getElementById("preview");
const statusBox = document.getElementById("status");
const answerBox = document.getElementById("answer");
const rawBox = document.getElementById("raw");

imageInput.addEventListener("change", () => {
  const file = imageInput.files[0];
  if (!file) return;
  preview.src = URL.createObjectURL(file);
  preview.style.display = "block";
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const file = imageInput.files[0];
  const question = document.getElementById("question").value;

  const data = new FormData();
  data.append("image", file);
  data.append("question", question);

  statusBox.innerHTML = "<p><b>Running PA-LLaVA...</b> This may take around 45–60 seconds on Mac.</p>";
  answerBox.style.display = "none";
  rawBox.textContent = "";

  try {
    const res = await fetch("/predict", {
      method: "POST",
      body: data
    });

    const json = await res.json();

    if (!res.ok) {
      throw new Error(JSON.stringify(json, null, 2));
    }

    statusBox.innerHTML = "<p><b>Done.</b> Elapsed: " + json.elapsed_seconds + " seconds</p>";
    answerBox.textContent = "Answer: " + (json.answer || "(No parsed answer)");
    answerBox.style.display = "block";
    rawBox.textContent = JSON.stringify(json, null, 2);

  } catch (err) {
    statusBox.innerHTML = "<p><b>Error</b></p>";
    rawBox.textContent = err.toString();
  }
});
</script>
</body>
</html>
    """
