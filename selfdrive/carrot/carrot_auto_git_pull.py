#!/usr/bin/env python3
"""
v: 재억 요청(2026-08-23) - c3-wip 브랜치(같은 계정의 다른 브랜치)에 있는 "웹 설정 ->
자동 업데이트" 기능을 my 브랜치에도 넣어달라는 요청. 원본은 135개 파일짜리 큰 웹
대시보드(터미널/블랙박스/화면녹화 등) 안에 딸려있어서 통째로 옮기는 건 실차 검증
없이 하기엔 위험함. 그래서 핵심 동작(설정 켜져있으면 새 커밋 생겼을 때 자동으로
git pull만 받고, 재부팅은 안 하고 다음에 사용자가 직접 재부팅)만 작은 독립 프로세스로
새로 만듦. 켜고 끄는 스위치는 웹 설정 페이지 대신 파라미터(CarrotAutoGitPull) 하나로
단순화. #문제시 원복
"""
import os
import subprocess
import time

from openpilot.common.params import Params
from openpilot.common.basedir import BASEDIR

POLL_INTERVAL = 60.0        # 몇 초마다 새 커밋 있는지 확인할지
COOLDOWN = 300.0            # 한 번 pull 받은 뒤 최소 이만큼(초)은 다시 안 받음
INITIAL_DELAY = 30.0        # 부팅 직후 바로 시작하지 않고 이만큼 대기(부팅 안정화)
GIT_TIMEOUT = 60.0


def _git(args: list[str], timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
  try:
    proc = subprocess.run(
      ["git", *args],
      cwd=BASEDIR,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      timeout=timeout,
    )
    return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace").strip()
  except subprocess.TimeoutExpired:
    return 124, "timeout"
  except Exception as exc:
    return 1, str(exc)


def _behind_count() -> int:
  rc, _ = _git(["fetch"], timeout=30.0)
  if rc != 0:
    return 0
  rc, out = _git(["rev-list", "--count", "HEAD..@{u}"])
  if rc != 0:
    return 0
  try:
    return int(out.strip())
  except ValueError:
    return 0


def _did_update(output: str) -> bool:
  body = (output or "").strip().lower()
  if not body or "already up to date" in body or "already up-to-date" in body:
    return False
  return "fast-forward" in body or "merge made by" in body or "updating " in body


def _run_git_pull() -> bool:
  # 수동 git pull 버튼과 동일한 동작: hard reset 후 pull. 재부팅은 하지 않음.
  _git(["reset", "--hard"], timeout=60.0)
  rc, out = _git(["pull"], timeout=120.0)
  ok = rc == 0 and _did_update(out)
  print(f"[carrot_auto_git_pull] pull rc={rc} updated={ok} out={out[:200]}", flush=True)
  return ok


def main() -> None:
  params = Params()
  time.sleep(INITIAL_DELAY)
  last_pull_at = 0.0
  while True:
    try:
      enabled = params.get_bool("CarrotAutoGitPull")
      # v: 재억 요청(2026-08-23) - 오토튜너 도입 때와 동일하게, 운전 중에는 절대 손대지
      # 않고 "정차/파킹 상태(Offroad)"일 때만 pull 받도록 조건 추가. #문제시 원복
      is_offroad = params.get_bool("IsOffroad")
      if enabled and is_offroad:
        behind = _behind_count()
        if behind > 0 and (time.time() - last_pull_at) >= COOLDOWN:
          print(f"[carrot_auto_git_pull] behind {behind} commit(s) -> git pull", flush=True)
          last_pull_at = time.time()
          _run_git_pull()
    except Exception as exc:
      print(f"[carrot_auto_git_pull] loop error: {exc}", flush=True)
    time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
  main()
