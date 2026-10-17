#!/usr/bin/env python3
"""
v: 재억 요청(2026-10-17) - "깜빡이 켜기"를 켜면 캔 오류가 뜨는 원인 확인용 읽기 전용 기록 프로그램.
의심: 우리가 보내는 SPAS1(357)/SPAS2(362) 신호를, 차 안의 주차보조 장치(APRK)도 같은 이름표로 보내고 있어서
통신선에서 부딪히는 것. 그래서 차가 이 두 신호를 스스로 보내는지(몇 번씩, 어느 통신선으로) 5초 단위로 기록한다.

- 아무것도 보내지 않고 받기만 한다(차·콤마 동작에 영향 없음). 어떤 오류가 나도 프로그램만 조용히 다시 시도한다.
- 경고 문구가 바뀔 때마다 그 순간도 같이 기록해서, 경고 시각과 신호 개수를 맞춰볼 수 있다.
- 통신선 번호(bus)가 128 이상이면 우리가 보낸 신호가 되돌아온 것일 수 있음(차가 스스로 보낸 것과 구분해서 볼 것)
- 기록 파일: /data/community/crashes/spas_probe.log  (플릿매니저 8082 포트의 error_logs 화면에서 볼 수 있음)
문제시 원복
"""
import os
import time

import cereal.messaging as messaging

LOG_DIR = "/data/community/crashes"
LOG_PATH = os.path.join(LOG_DIR, "spas_probe.log")
SPAS_ADDRS = (357, 362)      # SPAS1, SPAS2
WINDOW_SEC = 5.0             # 몇 초마다 세어서 기록할지
HEARTBEAT_SEC = 60.0         # 아무 일 없어도 이만큼마다 한 줄은 남김(프로그램이 살아있다는 표시)
MAX_BYTES = 400_000          # 파일이 이보다 커지면 뒤쪽만 남김
KEEP_BYTES = 200_000


def _stamp() -> str:
  return time.strftime("%m-%d %H:%M:%S", time.localtime())


def _write(line: str) -> None:
  try:
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
      if os.path.getsize(LOG_PATH) > MAX_BYTES:
        with open(LOG_PATH, "rb") as f:
          f.seek(-KEEP_BYTES, os.SEEK_END)
          tail = f.read()
        with open(LOG_PATH, "wb") as f:
          f.write(tail)
    except OSError:
      pass
    with open(LOG_PATH, "a", encoding="utf-8") as f:
      f.write(f"{_stamp()} {line}\n")
  except Exception:
    pass


def _safe(fn, default=None):
  try:
    return fn()
  except Exception:
    return default


def main() -> None:
  while True:
    try:
      run()
    except Exception as e:  # 어떤 오류든 프로그램이 죽지 않게 한다
      _write(f"probe error: {type(e).__name__}: {e}")
      time.sleep(5)


def run() -> None:
  can_sock = messaging.sub_sock("can", timeout=200)
  sm = messaging.SubMaster(["carState", "selfdriveState"])
  _write("probe start (SPAS1=357, SPAS2=362 수신 기록 시작)")

  counts: dict[tuple[int, int], int] = {}     # (통신선 번호, 이름표) -> 받은 개수
  total_frames = 0
  sample: dict[tuple[int, int], str] = {}     # 처음 받은 내용 앞부분(0으로 가득인지 확인용)
  window_start = time.monotonic()
  last_heartbeat = window_start
  last_alert = ""

  while True:
    msgs = messaging.drain_sock(can_sock, wait_for_one=True)
    for evt in msgs:
      for c in evt.can:
        total_frames += 1
        if c.address in SPAS_ADDRS:
          key = (int(c.src), int(c.address))
          counts[key] = counts.get(key, 0) + 1
          if key not in sample:
            sample[key] = bytes(c.dat[:12]).hex()

    sm.update(0)
    alert = _safe(lambda: (sm["selfdriveState"].alertText1 + " | " + sm["selfdriveState"].alertText2).strip(" |"), "") or ""
    now = time.monotonic()

    state = ""
    if sm.seen["carState"]:
      cs = sm["carState"]
      state = (f"gear={_safe(lambda: str(cs.gearShifter), '?')} L={int(_safe(lambda: cs.leftBlinker, 0))} "
               f"R={int(_safe(lambda: cs.rightBlinker, 0))} canValid={int(_safe(lambda: cs.canValid, 1))} "
               f"canTimeout={int(_safe(lambda: cs.canTimeout, 0))}")

    if alert != last_alert:
      last_alert = alert
      _write(f"ALERT '{alert}' | {state} | frames={total_frames} spas={dict(counts)}")

    if now - window_start >= WINDOW_SEC:
      if counts or now - last_heartbeat >= HEARTBEAT_SEC:
        last_heartbeat = now
        spas = {f"bus{b}:{a}": n for (b, a), n in sorted(counts.items())}
        _write(f"window {int(now - window_start)}s frames={total_frames} spas={spas or '없음'} sample={sample or '-'} | {state}")
      counts = {}
      total_frames = 0
      window_start = now


if __name__ == "__main__":
  main()
