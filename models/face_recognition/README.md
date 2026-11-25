# Face recognition model assets

This directory is intentionally reserved for the official OpenCV Zoo model files used by the attendance system.

Required files:
- face_detection_yunet_2023mar.onnx
- face_recognition_sface_2021dec.onnx

Official sources:
- YuNet: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- SFace: https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface

The project does not auto-download large model files on startup. Download them explicitly with the helper script:

```powershell
python scripts/download_face_models.py
```

Keep the model files in this directory so the service layer can lazily initialize the OpenCV YuNet and SFace pipeline when the deployment is configured.

License and attribution requirements:
- Respect the upstream OpenCV Zoo license terms.
- Keep source attribution and provenance available for administrators.
- Do not overwrite valid model files without confirming the replacement is intentional.

The service layer intentionally refuses to operate without these files and a valid Fernet key.
