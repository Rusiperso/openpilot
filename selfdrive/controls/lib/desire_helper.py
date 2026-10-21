from cereal import log
from openpilot.common.conversions import Conversions as CV
from openpilot.common.realtime import DT_MDL
import numpy as np
from openpilot.common.params import Params

from openpilot.selfdrive.controls.lib.desire_lib.constants import (
  LaneChangeState, LaneChangeDirection, TurnDirection,
  LANE_CHANGE_SPEED_MIN, LANE_CHANGE_TIME_MAX,
  BLINKER_NONE, BLINKER_LEFT, BLINKER_RIGHT,
  DESIRES, TURN_DESIRES
)
from openpilot.selfdrive.controls.lib.desire_lib.side_state import SideState
from openpilot.selfdrive.controls.lib.desire_lib.maneuver_classifier import classify_maneuver_type

# 2026-10-17: 시스템(ATC/Carrot)이 직접 깜빡이를 켜고 하는 차선변경은 깜빡이를 최소 3번 깜빡인 뒤에 시작한다.
# 깜빡이 한 번이 약 0.7~0.8초라 3번이면 약 2.2초, 램프가 켜지기까지 걸리는 시간을 더해 2.5초로 잡음. 실차 확인 전. 문제시 원복
SYS_BLINK_MIN_SEC = 2.5
# 시스템 차선변경이 시작 조건(빈 차선 등)을 못 맞춰 계속 기다리면 이 시간 뒤에 포기하고 깜빡이를 끈다.
SYS_LC_WAIT_MAX_SEC = 10.0


class DesireHelper:
  def __init__(self):
    self.params = Params()
    self.frame = 0

    # FSM core
    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.lane_change_ll_prob = 1.0
    self.lane_change_delay = 0.0
    self.maneuver_type = "none"  # "none" / "turn" / "lane_change"

    self.desire = log.Desire.none
    self.turn_direction = TurnDirection.none
    self.enable_turn_desires = True
    self.turn_desire_state = False
    self.desire_disable_count = 0
    self.turn_disable_count = 0

    # per-side states
    self.left = SideState("left")
    self.right = SideState("right")

    # blinker/ATC state (원본 변수들 유지)
    self.blinker_ignore = False
    self.driver_blinker_state = BLINKER_NONE
    self.carrot_blinker_state = BLINKER_NONE
    self.carrot_lane_change_count = 0
    self.carrot_cmd_index_last = 0
    self.atc_type = ""
    self.atc_active = 0  # 0: 없음, 1: ATC 동작, 2: 충돌

    # 2026-10-17: 시스템이 켠 깜빡이 차선변경 보호 (램프가 켜지면 그 램프가 다시 "운전자 깜빡이"로 읽혀서 생기던 문제 방지)
    self.sys_lc_dir = BLINKER_NONE      # 시스템이 시작한 차선변경의 방향(없으면 BLINKER_NONE)
    self.sys_lc_timer = 0.0             # 시스템 깜빡이를 켠 뒤 흐른 시간(초)
    self.sys_echo_ignore = False        # 차선변경이 끝난 뒤 우리가 켠 램프가 꺼질 때까지 운전자 깜빡이로 안 봄
    self.sys_done_atc_type = ""         # 이미 끝난 ATC 종류(같은 종류가 이어지는 동안 다시 시작하지 않음)

    # auto lane change
    self.auto_lane_change_enable = False
    self.next_lane_change = False

    # keep pulse
    self.keep_pulse_timer = 0.0

    # params
    self.laneChangeNeedTorque = 0
    self.laneChangeBsd = 0
    self.laneLineCheck = 0
    self.laneChangeDelay = 0.0
    self.modelTurnSpeedFactor = 0.0
    self.model_turn_speed = 200.0

    # misc
    self.prev_desire_enabled = False
    self.desireLog = ""

    # externally readable flags
    self.lane_change_available_left = False
    self.lane_change_available_right = False

  # ─────────────────────────────────────────────
  # params/model
  # ─────────────────────────────────────────────
  def _update_params_periodic(self):
    if self.frame % 100 == 0:
      self.laneChangeNeedTorque = self.params.get_int("LaneChangeNeedTorque")
      self.laneChangeBsd = self.params.get_int("LaneChangeBsd")
      self.laneLineCheck = self.params.get_int("LaneLineCheck")
      self.laneChangeDelay = self.params.get_float("LaneChangeDelay") * 0.1
      self.modelTurnSpeedFactor = self.params.get_float("ModelTurnSpeedFactor") * 0.1

  def _make_model_turn_speed(self, modeldata):
    if self.modelTurnSpeedFactor > 0:
      model_turn_speed = np.interp(self.modelTurnSpeedFactor,
                                   modeldata.velocity.t,
                                   modeldata.velocity.x) * CV.MS_TO_KPH * 1.2
      self.model_turn_speed = self.model_turn_speed * 0.9 + model_turn_speed * 0.1
    else:
      self.model_turn_speed = 200.0

  def _check_desire_state(self, modeldata, carstate, maneuver_type):
    desire_state = modeldata.meta.desireState
    orientation_rate = abs(modeldata.orientationRate.z[5])
    orientation_rate_future = abs(modeldata.orientationRate.z[15])

    self.turn_desire_state = (desire_state[1] + desire_state[2]) > 0.1

    if maneuver_type == "turn" and abs(carstate.steeringAngleDeg) > 80 and orientation_rate_future < orientation_rate:
      self.turn_disable_count = int(10.0 / DT_MDL)
    else:
      self.turn_disable_count = max(0, self.turn_disable_count - 1)

  # ─────────────────────────────────────────────
  # blinkers/ATC (원본 로직 유지, side 계산은 별개)
  # ─────────────────────────────────────────────
  def _release_sys_lc(self, ignore_echo=True):
    """시스템이 켠 깜빡이 차선변경의 기억을 지운다. ignore_echo가 True면 남아 있는 우리 램프를 운전자 깜빡이로 오인하지 않는다."""
    self.sys_lc_dir = BLINKER_NONE
    self.sys_lc_timer = 0.0
    if ignore_echo:
      self.sys_echo_ignore = True
      self.sys_done_atc_type = self.atc_type

  def _update_driver_blinker(self, carstate):
    st = carstate.leftBlinker * 1 + carstate.rightBlinker * 2
    changed = st != self.driver_blinker_state
    self.driver_blinker_state = st

    enabled = st in (BLINKER_LEFT, BLINKER_RIGHT)
    if self.laneChangeNeedTorque < 0:
      enabled = False
    return st, changed, enabled

  def _update_atc_blinker(self, carrotMan, driver_blinker_state):
    atc_type = carrotMan.atcType
    atc_blinker_state = BLINKER_NONE

    # 유지 카운트는 DesireHelper에서 관리
    if self.carrot_lane_change_count > 0:
      atc_blinker_state = self.carrot_blinker_state
    elif carrotMan.carrotCmdIndex != self.carrot_cmd_index_last and carrotMan.carrotCmd == "LANECHANGE":
      self.carrot_cmd_index_last = carrotMan.carrotCmdIndex
      self.carrot_lane_change_count = int(0.2 / DT_MDL)
      self.carrot_blinker_state = BLINKER_LEFT if carrotMan.carrotArg == "LEFT" else BLINKER_RIGHT
      atc_blinker_state = self.carrot_blinker_state
    elif atc_type in ("turn left", "turn right"):
      if self.atc_active != 2:
        atc_blinker_state = BLINKER_LEFT if atc_type == "turn left" else BLINKER_RIGHT
        self.atc_active = 1
        self.blinker_ignore = False
    elif atc_type in ("fork left", "fork right", "atc left", "atc right"):
      if self.atc_active != 2:
        atc_blinker_state = BLINKER_LEFT if atc_type in ("fork left", "atc left") else BLINKER_RIGHT
        self.atc_active = 1
    else:
      self.atc_active = 0

    # 충돌 시 ATC 무효
    if driver_blinker_state != BLINKER_NONE and atc_blinker_state != BLINKER_NONE and driver_blinker_state != atc_blinker_state:
      atc_blinker_state = BLINKER_NONE
      self.atc_active = 2

    atc_desire_enabled = atc_blinker_state in (BLINKER_LEFT, BLINKER_RIGHT)

    # blinker_ignore
    if driver_blinker_state == BLINKER_NONE:
      self.blinker_ignore = False
    if self.blinker_ignore:
      atc_blinker_state = BLINKER_NONE
      atc_desire_enabled = False

    # 타입 변경 1프레임 무시
    if self.atc_type != atc_type:
      atc_desire_enabled = False
    self.atc_type = atc_type

    return atc_blinker_state, atc_desire_enabled

  # ─────────────────────────────────────────────
  # per-side processing (핵심: 좌/우 모두 매 프레임 계산)
  # ─────────────────────────────────────────────
  def _process_sides(self, carstate, modeldata, radarState):
    # geometry (좌/우)
    # left: outer laneLines[0], current laneLines[1], edge[0], cur_prob laneLineProbs[1]
    self.left.update_lane_geometry(
      modeldata.laneLines[0], modeldata.laneLineProbs[0],
      modeldata.laneLines[1],
      modeldata.roadEdges[0],
      cur_prob=modeldata.laneLineProbs[1],
    )
    # right: outer laneLines[3], current laneLines[2], edge[1], cur_prob laneLineProbs[2]
    self.right.update_lane_geometry(
      modeldata.laneLines[3], modeldata.laneLineProbs[3],
      modeldata.laneLines[2],
      modeldata.roadEdges[1],
      cur_prob=modeldata.laneLineProbs[2],
    )

    # lane line info (HUD용 raw는 기존대로 leftLaneLine/rightLaneLine)
    self.left.update_lane_line_info(carstate.leftLaneLine)
    self.right.update_lane_line_info(carstate.rightLaneLine)

    # BSD 설정
    ignore_bsd = (self.laneChangeBsd < 0)

    # obstacles
    v_ego = carstate.vEgo
    self.left.update_obstacles(v_ego, radarState.leadLeft, carstate.leftBlindspot, ignore_bsd, bsd_hold_sec=2.0)
    self.right.update_obstacles(v_ego, radarState.leadRight, carstate.rightBlindspot, ignore_bsd, bsd_hold_sec=2.0)

    # compute available (include BSD+object)
    if self.laneLineCheck >= 1:
      left_line_ok = self.left.lane_line_info_mod in (0, 5)
      right_line_ok = self.right.lane_line_info_mod in (0, 5)
    else:
      left_line_ok = self.left.lane_line_info_raw < 20
      right_line_ok = self.right.lane_line_info_raw < 20
    self.left.compute_lane_change_available(lane_line_info_lt_20=left_line_ok, ignore_bsd=ignore_bsd)
    self.right.compute_lane_change_available(lane_line_info_lt_20=right_line_ok, ignore_bsd=ignore_bsd)

    self.left.update_triggers()
    self.right.update_triggers()

    # externally readable
    self.lane_change_available_left = self.left.lane_change_available
    self.lane_change_available_right = self.right.lane_change_available

  def _get_selected_side(self, blinker_state: int) -> SideState:
    return self.left if blinker_state == BLINKER_LEFT else self.right

  # ─────────────────────────────────────────────
  # main update
  # ─────────────────────────────────────────────
  def update(self, carstate, modeldata, lateral_active, lane_change_prob, carrotMan, radarState):
    self.frame += 1
    self._update_params_periodic()
    self._make_model_turn_speed(modeldata)

    # counts
    self.carrot_lane_change_count = max(0, self.carrot_lane_change_count - 1)
    self.lane_change_delay = max(0.0, self.lane_change_delay - DT_MDL)

    v_ego = carstate.vEgo
    below_lane_change_speed = v_ego < LANE_CHANGE_SPEED_MIN

    # per-side compute (좌/우 모두)
    self._process_sides(carstate, modeldata, radarState)

    # desire state from model
    self._check_desire_state(modeldata, carstate, self.maneuver_type)

    # blinkers
    driver_st, driver_changed, driver_enabled = self._update_driver_blinker(carstate)
    atc_st, atc_enabled = self._update_atc_blinker(carrotMan, driver_st)

    # ── 시스템이 켠 깜빡이 차선변경 보호 (2026-10-17) ──
    # 시스템이 깜빡이를 켜면 차 램프가 켜지고, 그 램프가 다시 "운전자 깜빡이"로 읽혀서
    # (1) 깜빡이를 한 번 켜자마자 차선변경이 시작되고 (2) 차선변경이 끝나도 램프가 요청을 붙잡아 깜빡이가 안 꺼졌다.
    # 시스템이 시작한 차선변경은 방향을 기억해 두고, 같은 방향 램프는 우리 신호의 메아리로 보아 운전자 깜빡이로 치지 않는다. 문제시 원복
    if self.sys_done_atc_type:
      if self.atc_type != self.sys_done_atc_type:
        self.sys_done_atc_type = ""
      else:
        atc_st = BLINKER_NONE
        atc_enabled = False
    if self.sys_echo_ignore:
      if driver_st == BLINKER_NONE:
        self.sys_echo_ignore = False
      else:
        driver_enabled = False
    if self.sys_lc_dir != BLINKER_NONE:
      if driver_st not in (BLINKER_NONE, self.sys_lc_dir):
        # 반대 방향 깜빡이는 운전자가 끼어든 것 - 시스템 차선변경을 접고 운전자에게 넘긴다
        self._release_sys_lc(ignore_echo=False)
      else:
        driver_enabled = False
        atc_st = self.sys_lc_dir
        atc_enabled = True
        self.sys_lc_timer += DT_MDL
        if self.lane_change_state == LaneChangeState.preLaneChange and self.sys_lc_timer > SYS_LC_WAIT_MAX_SEC:
          # 오래 기다려도 시작 조건이 안 맞으면 포기하고 깜빡이를 끈다
          self.lane_change_state = LaneChangeState.off
          self.lane_change_direction = LaneChangeDirection.none
          self._release_sys_lc()
          atc_st = BLINKER_NONE
          atc_enabled = False
    sys_lc_active = self.sys_lc_dir != BLINKER_NONE

    desire_enabled = driver_enabled or atc_enabled
    blinker_state = driver_st if driver_enabled else atc_st

    # 선택된 side (FSM은 이 side만 참고)
    side = self._get_selected_side(blinker_state) if blinker_state in (BLINKER_LEFT, BLINKER_RIGHT) else None

    # auto lane change trigger (기존 로직 유지하되 side 기반)
    auto_lane_change_trigger = False
    if desire_enabled and side is not None:
      # carrot_lane_change_count>0이면 강제 허용
      if self.carrot_lane_change_count > 0:
        auto_lane_change_trigger = side.lane_change_available
      else:
        # 기존 조건: edge_available + (trigger or appeared) + not side_object_detected
        auto_lane_change_trigger = (
          self.auto_lane_change_enable and
          side.edge_available and
          (side.lane_available_trigger or side.lane_appeared) and
          (not side.side_object_detected) and
          (side.bsd_hold_counter == 0)
        )
      self.desireLog = (
        f"{side.name}:ALC={self.auto_lane_change_enable}, "
        f"L={side.lane_available},E={side.edge_available}, "
        f"T={side.lane_available_trigger},A={side.lane_appeared}, "
        f"OBJ={side.side_object_detected},BSD={side.bsd_hold_counter>0}"
      )
    else:
      self.auto_lane_change_enable = False
      self.next_lane_change = False

    # ───────────────────────── FSM ─────────────────────────
    if not lateral_active or self.lane_change_timer > LANE_CHANGE_TIME_MAX:
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
      self.turn_direction = TurnDirection.none
      self.maneuver_type = "none"

    elif self.desire_disable_count > 0:
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
      self.turn_direction = TurnDirection.none
      self.maneuver_type = "none"

    else:
      # classify maneuver type using selected side
      if desire_enabled and side is not None:
        new_type = classify_maneuver_type(
          blinker_state=blinker_state,
          carstate=carstate,
          side=side,
          turn_desire_state=self.turn_desire_state,
          atc_type=self.atc_type,
          old_type=self.maneuver_type,
        )
      else:
        new_type = "none"

      # switching rules
      if self.maneuver_type == "lane_change" and new_type == "turn" and self.lane_change_state not in (
        LaneChangeState.preLaneChange, LaneChangeState.laneChangeStarting
      ):
        self.maneuver_type = "turn"
        self.lane_change_state = LaneChangeState.off
      elif self.lane_change_state in (LaneChangeState.off, LaneChangeState.preLaneChange):
        self.maneuver_type = new_type

      # ─ TURN mode ─
      if desire_enabled and self.maneuver_type == "turn" and self.enable_turn_desires:
        self.lane_change_state = LaneChangeState.off
        if self.turn_disable_count > 0:
          self.turn_direction = TurnDirection.none
          self.lane_change_direction = LaneChangeDirection.none
        else:
          self.turn_direction = TurnDirection.turnLeft if blinker_state == BLINKER_LEFT else TurnDirection.turnRight
          self.lane_change_direction = self.turn_direction

      # ─ Lane change FSM ─
      else:
        self.turn_direction = TurnDirection.none

        if self.lane_change_state == LaneChangeState.off:
          if desire_enabled and not self.prev_desire_enabled and not below_lane_change_speed and side is not None:
            self.lane_change_state = LaneChangeState.preLaneChange
            self.lane_change_ll_prob = 1.0
            self.lane_change_delay = self.laneChangeDelay

            # 맨 끝 차선이 아니면, ATC 자동 차선변경 비활성
            # (원본 유지: 차선 존재하거나 geom 가능하면 auto off, 아니면 on)
            lane_exist_counter_side = side.lane_exist_count.counter
            lane_change_available_geom = side.lane_change_available_geom
            self.auto_lane_change_enable = False if (lane_exist_counter_side > 0 or lane_change_available_geom) else True
            self.next_lane_change = False

            # 시스템(ATC/Carrot)이 켠 깜빡이면 방향을 기억 - 이후 램프가 켜져도 운전자 깜빡이로 오인하지 않게
            if atc_enabled and not driver_enabled and atc_st in (BLINKER_LEFT, BLINKER_RIGHT):
              self.sys_lc_dir = atc_st
              self.sys_lc_timer = 0.0

        elif self.lane_change_state == LaneChangeState.preLaneChange:
          if side is None:
            self.lane_change_state = LaneChangeState.off
            self.lane_change_direction = LaneChangeDirection.none
          else:
            self.lane_change_direction = LaneChangeDirection.left if blinker_state == BLINKER_LEFT else LaneChangeDirection.right

            # torque direction cond
            torque_cond = (carstate.steeringTorque > 0) if blinker_state == BLINKER_LEFT else (carstate.steeringTorque < 0)
            torque_applied = carstate.steeringPressed and torque_cond

            # BSD config
            ignore_bsd = (self.laneChangeBsd < 0)
            block_lanechange_bsd = (self.laneChangeBsd == 1)
            bsd_active = (side.bsd_hold_counter > 0) and (not ignore_bsd)

            # 차선이 일정시간 이상 안보이면 auto 허용(원본 유지)
            if (not side.lane_available) or (side.lane_exist_count.counter < int(2.0 / DT_MDL)):
              self.auto_lane_change_enable = True

            if not desire_enabled or below_lane_change_speed:
              self.lane_change_state = LaneChangeState.off
              self.lane_change_direction = LaneChangeDirection.none
            else:
              # 차선변경 시작 조건:
              # - side.lane_change_available는 BSD+object 포함(요구사항)
              # - 하지만 BSD 중에도 torque override 허용해야 하므로, BSD 분기를 별도로 둠(원본 동작 유지)
              # LaneLineCheck=2: 실선에서도 토크 override 허용
              solid_line_blocked = (self.laneLineCheck >= 2) and (not side.lane_change_available_geom) and \
                                   (side.lane_available or side.edge_available)
              start_gate = (side.lane_change_available_geom and self.lane_change_delay == 0) or \
                           side.lane_line_info_edge_detect or solid_line_blocked

              # 시스템이 켠 깜빡이는 최소 3번 깜빡인 뒤에 차선변경 시작 (2026-10-17)
              blink_ready = (not sys_lc_active) or self.sys_lc_timer >= SYS_BLINK_MIN_SEC

              if start_gate and blink_ready:
                if solid_line_blocked:
                  if torque_applied and not (bsd_active and block_lanechange_bsd):
                    self.lane_change_state = LaneChangeState.laneChangeStarting
                elif bsd_active:
                  if torque_applied and (not block_lanechange_bsd):
                    self.lane_change_state = LaneChangeState.laneChangeStarting
                elif self.laneChangeNeedTorque > 0 or self.next_lane_change:
                  if torque_applied:
                    self.lane_change_state = LaneChangeState.laneChangeStarting
                elif driver_enabled or sys_lc_active:
                  # driver blinker면 바로 시작(원본 유지). 시스템이 켠 깜빡이도 같은 취급(깜빡임 횟수는 blink_ready가 확인)
                  # 단, object/bzd 막힘은 side.lane_change_available에서 걸림
                  if side.lane_change_available:
                    self.lane_change_state = LaneChangeState.laneChangeStarting
                else:
                  if torque_applied or auto_lane_change_trigger or side.lane_line_info_edge_detect:
                    # 여기서는 시작 직전 안전성 체크
                    if side.lane_change_available:
                      self.lane_change_state = LaneChangeState.laneChangeStarting

        elif self.lane_change_state == LaneChangeState.laneChangeStarting:
          self.lane_change_ll_prob = max(self.lane_change_ll_prob - 2 * DT_MDL, 0.0)
          if lane_change_prob < 0.02 and self.lane_change_ll_prob < 0.01:
            self.lane_change_state = LaneChangeState.laneChangeFinishing

        elif self.lane_change_state == LaneChangeState.laneChangeFinishing:
          self.lane_change_ll_prob = min(self.lane_change_ll_prob + DT_MDL, 1.0)
          if self.lane_change_ll_prob > 0.99:
            self.lane_change_direction = LaneChangeDirection.none
            if sys_lc_active:
              # 시스템이 켠 깜빡이로 한 차선변경은 끝나면 깜빡이를 끈다 (자동 반복 없음) (2026-10-17)
              self.lane_change_state = LaneChangeState.off
              self.next_lane_change = False
              self._release_sys_lc()
            elif desire_enabled:
              self.lane_change_state = LaneChangeState.preLaneChange
              self.next_lane_change = True
            else:
              self.lane_change_state = LaneChangeState.off

    # 시스템 차선변경이 어떤 이유로든(취소·속도·시간 초과 등) off가 되면 기억도 지운다 (2026-10-17)
    if self.sys_lc_dir != BLINKER_NONE and self.lane_change_state == LaneChangeState.off:
      self._release_sys_lc()

    # timer
    if self.lane_change_state in (LaneChangeState.off, LaneChangeState.preLaneChange):
      self.lane_change_timer = 0.0
    else:
      self.lane_change_timer += DT_MDL

    # commit last per-side
    self.left.commit_last()
    self.right.commit_last()

    self.prev_desire_enabled = desire_enabled

    # 반대 방향 토크로 cancel (기존 유지)
    steering_pressed_cancel = carstate.steeringPressed and (
      (carstate.steeringTorque < 0 and blinker_state == BLINKER_LEFT) or
      (carstate.steeringTorque > 0 and blinker_state == BLINKER_RIGHT)
    )
    if steering_pressed_cancel and self.lane_change_state != LaneChangeState.off:
      self.lane_change_direction = LaneChangeDirection.none
      self.lane_change_state = LaneChangeState.off
      self.blinker_ignore = True

    # final desire
    if self.turn_direction != TurnDirection.none:
      self.desire = TURN_DESIRES[self.turn_direction]
      self.lane_change_direction = self.turn_direction
    else:
      self.desire = DESIRES[self.lane_change_direction][self.lane_change_state]

    # keep pulse
    if self.lane_change_state in (LaneChangeState.off, LaneChangeState.laneChangeStarting):
      self.keep_pulse_timer = 0.0
    elif self.lane_change_state == LaneChangeState.preLaneChange:
      self.keep_pulse_timer += DT_MDL
      if self.keep_pulse_timer > 1.0:
        self.keep_pulse_timer = 0.0
      elif self.desire in (log.Desire.keepLeft, log.Desire.keepRight):
        self.desire = log.Desire.none

    return self.desire
