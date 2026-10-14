"""Fleet Manager G4 endpoint without vehicle-runtime imports."""
from flask import current_app, jsonify

from openpilot.selfdrive.carrot.g4_upload import G4UploadError, upload_segment


def register(app, log_root, metadata_provider):
  @app.route("/footage/full/upload_g4/<route>/<segment>", methods=["POST"])
  def upload_g4(route, segment):
    try:
      result = upload_segment(log_root(), f"{route}--{segment}", metadata_provider())
      result["message"] = "G4 서버 저장 완료"
      return jsonify(result), 200
    except G4UploadError as exc:
      return jsonify(ok=False, target="g4", error=str(exc), filesSaved=exc.files_saved), exc.status
    except Exception:
      current_app.logger.exception("G4 upload failed")
      return jsonify(ok=False, error="G4 업로드 처리 중 오류가 발생했습니다. 기기 로그를 확인하세요."), 500
