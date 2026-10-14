from collections import deque
from pathlib import Path

import pytest
import requests
from flask import Flask

from openpilot.selfdrive.carrot import g4_upload
from openpilot.selfdrive.frogpilot.fleetmanager.g4_routes import register


SEGMENT = "00000cfb--69588de3d7--0"
META = {"carName": "TEST_CAR", "dongleId": "0123456789abcdef"}


class Response:
  def __init__(self, body, status=200):
    self.body = body
    self.status_code = status
    self.text = str(body)

  def json(self):
    return self.body


class Session:
  def __init__(self, responses):
    self.responses = deque(responses)
    self.calls = []

  def __enter__(self):
    return self

  def __exit__(self, *args):
    pass

  def request(self, method, url, **kwargs):
    data = kwargs.get("data")
    if hasattr(data, "read"):
      kwargs["data"] = data.read()
    self.calls.append((method, url, kwargs))
    response = self.responses.popleft()
    if isinstance(response, Exception):
      raise response
    return response


@pytest.fixture
def source(tmp_path):
  folder = tmp_path / SEGMENT
  folder.mkdir()
  (folder / "rlog.zst").write_bytes(b"full log")
  (folder / "qcamera.ts").write_bytes(b"road video")
  return folder


def install_session(monkeypatch, responses):
  monkeypatch.delenv("CARROT_G4_WEB_UPLOAD_URL", raising=False)
  session = Session(responses)
  monkeypatch.setattr(g4_upload.requests, "Session", lambda: session)
  return session


def success_responses():
  return [Response({"ok": True, "token": "test-session"}), Response({"ok": True, "size": 8}),
          Response({"ok": True, "size": 10}), Response({"ok": True})]


def test_upload_streams_original_files_and_records_completion(source, monkeypatch):
  (source / "dcamera.ts").write_bytes(b"excluded camera")
  (source / "private.json").write_bytes(b"excluded settings")
  session = install_session(monkeypatch, success_responses())
  result = g4_upload.upload_segment(source.parent, SEGMENT, META)
  assert result["ok"]
  assert result["remotePath"] == f"routes/TEST_CAR 0123456789abcdef/{SEGMENT}"
  puts = [(url, args) for method, url, args in session.calls if method == "PUT"]
  assert [args["data"] for _, args in puts] == [b"full log", b"road video"]
  assert [url.rsplit("/", 1)[1] for url, _ in puts] == ["rlog.zst", "qcamera.ts"]
  assert session.calls[-1][2]["json"]["results"][0]["files"] == result["files"]


@pytest.mark.parametrize("case", ["missing-rlog", "empty-rlog", "locked", "linked-rlog", "too-large"])
def test_bad_source_is_rejected_before_any_network_request(source, monkeypatch, case):
  rlog = source / "rlog.zst"
  if case == "missing-rlog":
    rlog.unlink()
  elif case == "empty-rlog":
    rlog.write_bytes(b"")
  elif case == "locked":
    (source / "rlog.zst.lock").touch()
  elif case == "linked-rlog":
    rlog.unlink()
    rlog.symlink_to(source / "qcamera.ts")
  elif case == "too-large":
    with rlog.open("wb") as stream:
      stream.truncate(g4_upload.MAX_FILE_BYTES + 1)
  session = install_session(monkeypatch, [])
  with pytest.raises(g4_upload.G4UploadError):
    g4_upload.upload_segment(source.parent, SEGMENT, META)
  assert not session.calls


def test_alternate_log_format_and_log_only_upload_are_supported(source):
  (source / "rlog.zst").rename(source / "rlog.bz2")
  (source / "qcamera.ts").unlink()
  assert [path.name for path in g4_upload.selected_files(source.parent, SEGMENT)] == ["rlog.bz2"]


def test_serial_fallback_is_also_used_for_completion_identity(source, monkeypatch):
  session = install_session(monkeypatch, success_responses())
  g4_upload.upload_segment(source.parent, SEGMENT, {**META, "dongleId": "bad", "serial": "serial123456"})
  assert session.calls[0][2]["json"]["deviceId"] == "serial123456"
  assert session.calls[-1][2]["json"]["meta"]["dongleId"] == "serial123456"


@pytest.mark.parametrize("response", [Response({"ok": True, "size": 1}), Response({"ok": False}, 200),
                                    Response({"error": "quota exceeded"}, 413), requests.ConnectionError("offline")])
def test_failed_or_incorrect_file_response_never_reports_success(source, monkeypatch, response):
  session = install_session(monkeypatch, [Response({"ok": True, "token": "test-session"}), response])
  with pytest.raises(g4_upload.G4UploadError):
    g4_upload.upload_segment(source.parent, SEGMENT, META)
  assert not any(url.endswith("/complete") for _, url, _ in session.calls)


def test_completion_failure_reports_that_files_were_saved(source, monkeypatch):
  responses = success_responses()
  responses[-1] = Response({"error": "manifest storage failed"}, 500)
  install_session(monkeypatch, responses)
  with pytest.raises(g4_upload.G4UploadError) as result:
    g4_upload.upload_segment(source.parent, SEGMENT, META)
  assert result.value.files_saved
  assert "완료 기록" in str(result.value)


def test_flask_endpoint_allows_retry_instead_of_caching_a_segment(source, monkeypatch):
  install_session(monkeypatch, success_responses() * 2)
  app = Flask(__name__)
  register(app, lambda: str(source.parent), lambda: META)
  with app.test_client() as client:
    for _ in range(2):
      response = client.post("/footage/full/upload_g4/00000cfb--69588de3d7/0")
      assert response.status_code == 200
      assert response.json["ok"]


def test_flask_endpoint_returns_request_scoped_failure(source, monkeypatch):
  install_session(monkeypatch, [Response({"error": "receiver unavailable"}, 503)])
  app = Flask(__name__)
  register(app, lambda: str(source.parent), lambda: META)
  with app.test_client() as client:
    response = client.post("/footage/full/upload_g4/00000cfb--69588de3d7/0")
    assert response.status_code == 502
    assert not response.json["ok"]
    assert "receiver unavailable" in response.json["error"]


def test_tmux_upload_uses_its_own_session_and_keeps_file_handles_open(tmp_path, monkeypatch):
  log = tmp_path / "tmux.log"
  log.write_text("dummy tmux")
  session = install_session(monkeypatch, [Response({"ok": True, "token": "tmux-session"}), Response({"ok": True, "files": 1})])
  result = g4_upload.upload_tmux(log, {**META, "tmux_why": "onroad", "branch": "my"})
  assert result["ok"]
  assert session.calls[0][2]["json"]["purpose"] == "tmux"
  assert session.calls[-1][1].endswith("/api/v1/tmux/upload")
  assert session.calls[-1][2]["files"][0][0] == "files[0]"


@pytest.mark.parametrize("segment", ["../outside--0", "bad/route--0", "route--not-a-number"])
def test_path_traversal_and_invalid_segments_are_rejected(tmp_path, segment):
  with pytest.raises(g4_upload.G4UploadError):
    g4_upload.selected_files(tmp_path, segment)
