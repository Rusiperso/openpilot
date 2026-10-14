import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from openpilot.selfdrive.carrot import g4_upload


ROOT = Path(__file__).resolve().parents[4]


def test_carrot_web_g4_request_uses_the_shared_sender_without_discord(tmp_path):
  source = ROOT / "selfdrive/carrot/server/core.py"
  tree = ast.parse(source.read_text())
  names = {"api_dashcam_upload", "_dashcam_safe_segment", "_dashcam_route_name", "_dashcam_segment_index"}
  functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
  captured = []

  def send(root, segment, metadata):
    captured.append((root, segment, metadata))
    return {"ok": True, "files": [{"name": "rlog.zst", "size": 10}]}

  async def discord(*args):
    raise AssertionError("G4 upload must not send a Discord report")

  namespace = {"web": web, "asyncio": asyncio, "HAS_PARAMS": False, "DASHCAM_ROOT": str(tmp_path),
               "g4_metadata": g4_upload.normalize_metadata, "g4_storage_directory": g4_upload.storage_directory,
               "upload_g4_segment": send, "_dashcam_upload_metadata": lambda params: {"carName": "TEST", "dongleId": "0123456789abcdef"},
               "_dashcam_segment_dir": lambda segment: str(tmp_path / segment), "_dashcam_segment_file_summary": lambda folder: [],
               "_dashcam_upload_share_text": lambda payload: "dummy report", "_dashcam_send_discord_webhook": discord,
               "datetime": __import__("datetime").datetime}
  exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)

  class Request:
    async def json(self):
      return {"target": "g4", "segments": ["00000cfb--69588de3d7--0"]}

  response = asyncio.run(namespace["api_dashcam_upload"](Request()))
  body = json.loads(response.body)
  assert body["ok"] and body["target"] == "g4"
  assert body["discord"]["skipped"]
  assert captured[0][0] == str(tmp_path)
  assert body["results"][0]["files"] == [{"name": "rlog.zst", "size": 10}]


def test_manual_tmux_reports_g4_success_even_when_legacy_ftp_fails():
  source = ROOT / "selfdrive/carrot/carrot_man.py"
  tree = ast.parse(source.read_text())
  cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CarrotMan")
  function = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "carrot_cmd_zmq")
  branch = next(node for node in ast.walk(function) if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Constant) and node.test.left.value == "tmux_send")
  calls = []
  replies = []

  def legacy(*args):
    calls.append("legacy FTP")
    raise ConnectionError("legacy server is offline")

  client = SimpleNamespace(make_tmux_data=lambda: calls.append("capture"),
                           send_tmux_g4=lambda reason: calls.append("G4") or {"ok": True},
                           send_tmux=legacy, send_tmux_http=lambda reason: calls.append("legacy HTTP"))
  exec(compile(ast.Module(body=branch.body, type_ignores=[]), str(source), "exec"),
       {"self": client, "json": json, "json_obj": {"tmux_send": "dummy"}, "socket": SimpleNamespace(send=replies.append)})
  assert calls == ["capture", "G4", "legacy FTP", "legacy HTTP"]
  assert json.loads(replies[0])["g4_ok"]
