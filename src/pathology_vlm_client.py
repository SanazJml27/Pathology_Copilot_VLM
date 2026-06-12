import base64
import os
from typing import Optional

import requests


class PathologyVLMClient:
    def __init__(self):
        self.enabled = os.getenv("USE_PATHOLOGY_VLM", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        self.url = os.getenv(
            "PATHOLOGY_VLM_URL",
            "http://127.0.0.1:9000/predict",
        )

        self.timeout = int(os.getenv("PATHOLOGY_VLM_TIMEOUT", "1200"))

    def _decode_roi_snapshot(self, roi_snapshot: str) -> bytes:
        if not roi_snapshot:
            raise ValueError("Empty ROI snapshot.")

        if "," in roi_snapshot and roi_snapshot.startswith("data:"):
            _, encoded = roi_snapshot.split(",", 1)
        else:
            encoded = roi_snapshot

        return base64.b64decode(encoded)

    def ask_roi(
        self,
        roi_snapshot: str,
        user_prompt: str,
        ehr_context: Optional[str] = None,
        case_id: Optional[str] = None,
    ) -> dict:
        """
        Send only the selected ROI image and the user's visual question to PA-LLaVA.

        Important:
        We intentionally do NOT inject EHR context here, because clinical text can bias
        the visual model into describing the wrong organ/site.
        """
        image_bytes = self._decode_roi_snapshot(roi_snapshot)

        base_question = (user_prompt or "").strip()

        if not base_question:
            base_question = (
                "Could you provide a detailed description of what is shown in this selected pathology ROI?"
            )

        question = (
            f"{base_question}\n\n"
            "Please answer based on the selected pathology ROI image only. "
            "Do not infer the organ, disease type, or diagnosis from clinical history, file names, or external context. "
            "Describe visible tissue architecture, cellular morphology, stromal/background features, "
            "and likely histopathological interpretation when supported by the image. "
            "Avoid one-word or yes/no answers unless explicitly requested."
        )

        files = {
            "image": (
                f"{case_id or 'roi'}.jpg",
                image_bytes,
                "image/jpeg",
            )
        }

        data = {
            "question": question,
        }

        response = requests.post(
            self.url,
            files=files,
            data=data,
            timeout=self.timeout,
        )

        response.raise_for_status()
        result = response.json()

        return {
            "model": "PA-LLaVA",
            "answer": result.get("answer", ""),
            "raw_result": result,
        }
