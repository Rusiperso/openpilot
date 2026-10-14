"""G4 NAS uploads shared by Fleet Manager, Carrot Web and tmux diagnostics."""
from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

import requests


DEFAULT_G4_UPLOAD_URL = "https://upload.g4nas.my"
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.|-]{0,127}--\d+")
RLOG_NAMES = ("rlog.zst", "rlog.bz2", "rlog")
VIDEO_NAMES = ("qcamera.ts", "qcamera.mp4")
MAX_FILE_BYTES = 512 * 1024 * 1024


class G4UploadError(RuntimeError):
  def __init__(self, message: str, *, status: int = 502, files_saved: bool = False):
    super().__init__(message)
    self.status = status
    self.files_saved = files_saved


def _text(value: Any) -> str:
  if isinstance(value, bytes):
    value = value.decode("utf-8", errors="replace")
  return str(value or "").strip()


def _valid_device(value: str) -> bool:
  return bool(DEVICE_RE.fullmatch(value)) and value.lower() not in {"unknown", "none"}


def normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
  result = {str(key): _text(value)[:160] for key, value in metadata.items()}
  candidates = [result.get(key, "") for key in ("deviceId", "dongleId", "dongle_id", "serial", "device_serial")]
  device = next((value for value in candidates if _valid_device(value)), "")
  if not device:
    raise G4UploadError("유효한 DongleId 또는 기기 시리얼이 없습니다.", status=400)
  # Completion validates dongleId as well as deviceId. Both must name the
  # identity that was actually used to obtain the upload session.
  result["deviceId"] = device
  result["dongleId"] = device
  result["carName"] = result.get("carName") or result.get("car_name") or "none"
  return result


def current_metadata(params: Any, hardware_serial: Callable[[], Any] | None = None) -> dict[str, str]:
  def get(key: str) -> str:
    try:
      return _text(params.get(key))
    except Exception:
      return ""

  dongle = get("DongleId")
  serial = next(filter(None, (get(key) for key in ("HardwareSerial", "DeviceSerial", "Serial", "CarrotSerial"))), "")
  if not _valid_device(dongle) and not _valid_device(serial) and hardware_serial:
    try:
      serial = _text(hardware_serial())
    except Exception:
      pass
  return normalize_metadata({
    "dongleId": dongle, "serial": serial, "carName": get("CarName") or "none",
    "branch": get("GitBranch"), "commit": get("GitCommit"), "commitDate": get("GitCommitDate"),
  })


def base_url() -> str:
  url = os.environ.get("CARROT_G4_WEB_UPLOAD_URL", DEFAULT_G4_UPLOAD_URL).strip().rstrip("/")
  parsed = urlsplit(url)
  local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
  if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or not (parsed.scheme == "https" or local_http):
    raise G4UploadError("G4 업로드 서버는 올바른 HTTPS 주소여야 합니다.", status=400)
  return url


def _api(base: str, *parts: str) -> str:
  return base + "/api/v1/" + "/".join(quote(str(part), safe="") for part in parts)


def _json_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
  try:
    response = session.request(method, url, timeout=(10, 180), **kwargs)
  except requests.RequestException as exc:
    raise G4UploadError(f"G4 서버 연결 실패: {exc}") from exc
  try:
    body = response.json()
  except ValueError:
    body = {}
  if not 200 <= response.status_code < 300 or not isinstance(body, dict) or body.get("ok") is not True:
    detail = body.get("error") if isinstance(body, dict) else ""
    raise G4UploadError(f"G4 서버 HTTP {response.status_code}: {_text(detail or response.text)[:200]}")
  return body


def _session_token(session: requests.Session, base: str, metadata: dict[str, str], purpose: str) -> str:
  body = _json_request(session, "POST", _api(base, "session"), json={**metadata, "purpose": purpose})
  token = _text(body.get("token"))
  if not token:
    raise G4UploadError("G4 서버가 업로드 세션을 발급하지 않았습니다.")
  return token


def storage_directory(metadata: dict[str, str]) -> str:
  name = re.sub(r"[^A-Za-z0-9_. -]+", "_", metadata["carName"]).strip(" .")[:96] or "none"
  return f"{name} {metadata['deviceId']}"


def selected_files(log_root: str | Path, segment: str) -> list[Path]:
  if not SEGMENT_RE.fullmatch(segment):
    raise G4UploadError("잘못된 로그 구간입니다.", status=400)
  root = Path(log_root).resolve()
  folder = root / segment
  if folder.is_symlink() or not folder.is_dir() or root not in folder.resolve().parents:
    raise G4UploadError("로그 구간 폴더가 없습니다.", status=404)
  if any(folder.glob("*.lock")):
    raise G4UploadError("아직 녹화 중인 구간입니다. 주행 종료 후 다시 전송하세요.", status=409)
  files = []
  for names in (RLOG_NAMES, VIDEO_NAMES):
    path = next((folder / name for name in names if (folder / name).is_file()), None)
    if path is None:
      if names == RLOG_NAMES:
        raise G4UploadError("이 구간에 rlog가 없습니다. 전송하지 않았습니다.", status=400)
      continue
    if path.is_symlink():
      raise G4UploadError("링크 파일은 업로드할 수 없습니다.", status=400)
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
      raise G4UploadError(f"{path.name}: 파일이 비어 있거나 512 MiB 제한을 초과합니다.", status=400)
    files.append(path)
  return files


def upload_segment(log_root: str | Path, segment: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
  files = selected_files(log_root, segment)
  meta = normalize_metadata(metadata)
  base = base_url()
  uploaded = []
  with requests.Session() as session:
    token = _session_token(session, base, meta, "dashcam")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    for path in files:
      before = path.stat()
      with path.open("rb") as data:
        response = _json_request(session, "PUT", _api(base, "upload", meta["deviceId"], segment, path.name),
                                 data=data, headers={**headers, "X-File-Size": str(before.st_size), "Content-Length": str(before.st_size)})
      after = path.stat()
      if response.get("size") != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise G4UploadError(f"{path.name}: 전송 크기가 다르거나 전송 중 파일이 변경됐습니다.")
      uploaded.append({"name": path.name, "size": before.st_size})
    remote_base = f"routes/{storage_directory(meta)}/"
    result = {"ok": True, "target": "g4", "serverUrl": base, "deviceId": meta["deviceId"], "meta": meta,
              "uploaded": 1, "total": 1, "segment": segment, "remoteBasePath": remote_base,
              "remotePath": remote_base + segment, "files": uploaded}
    manifest = {**result, "results": [{"segment": segment, "ok": True, "files": uploaded}]}
    try:
      _json_request(session, "POST", _api(base, "complete"), json=manifest, headers={"Authorization": f"Bearer {token}"})
    except G4UploadError as exc:
      raise G4UploadError(f"파일은 저장됐지만 완료 기록 저장에 실패했습니다: {exc}", files_saved=True) from exc
    return result


def upload_tmux(log_path: str | Path, metadata: Mapping[str, Any], settings_path: str | Path | None = None) -> dict[str, Any]:
  path = Path(log_path)
  if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
    raise G4UploadError("전송할 tmux 로그가 없습니다.", status=400)
  meta = normalize_metadata(metadata)
  base = base_url()
  with requests.Session() as session, ExitStack() as stack:
    token = _session_token(session, base, meta, "tmux")
    files = [("files[0]", ("tmux.log", stack.enter_context(path.open("rb")), "text/plain"))]
    if settings_path is not None:
      settings = Path(settings_path)
      if not settings.is_file() or settings.is_symlink():
        raise G4UploadError("전송할 설정 파일이 없습니다.", status=400)
      files.append(("files[1]", ("toggle_values.json", stack.enter_context(settings.open("rb")), "application/json")))
    result = _json_request(session, "POST", _api(base, "tmux", "upload"),
                           headers={"Authorization": f"Bearer {token}"}, data=meta, files=files)
    if result.get("files") != len(files):
      raise G4UploadError("G4 서버에 저장된 tmux 파일 개수가 다릅니다.")
    return {**result, "target": "g4", "serverUrl": base}
