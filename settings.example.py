# 이 파일에서 입력 두 개와 API 키를 설정하세요.
# 상대 경로는 DevDay 폴더 기준입니다. 절대 경로도 사용할 수 있습니다.
# 기본 방식: input/normal.mp4, input/candidate.mp4에 영상을 넣으세요.
NORMAL_VIDEO = "input/normal.mp4"       # 정상 상태 영상 1개
CANDIDATE_VIDEO = "input/candidate.mp4" # 이상 여부를 확인할 영상 1개

# ── 웹 프로그램(start_web.command)에서 쓰는 AI ──
PROVIDER = "claude"            # "claude" 또는 "openai"
ANTHROPIC_API_KEY = ""         # Claude Console(platform.claude.com) > API Keys에서 만든 키
CLAUDE_MODEL = "claude-sonnet-5-5"  # 더 정확히: "claude-opus-5-5", 더 싸게: "claude-haiku-5-5"
ACCESS_CODE = ""               # 외부 공개(start_public.command) 시 접속 코드. 비우면 실행할 때마다 6자리 숫자를 새로 만듭니다.

OPENAI_API_KEY = ""  # OpenAI를 쓸 때만. run_analysis.command(터미널 실행)도 이 키를 씁니다.

# 저장/재생 FPS가 아닌 실제 촬영 FPS: 예) 240 또는 179.82
CAPTURE_FPS = None  # 반드시 실제 값으로 바꾸세요.
CANDIDATE_CAPTURE_FPS = None  # None이면 정상 영상과 같은 FPS 사용

# 실제로 같은 풍속 / 고정 카메라로 촬영했다면 True로 변경
SAME_SPEED = False
FIXED_CAMERA = False

# 실제 시연에 사용한 모델. 다른 지원 모델명으로 바꿀 수 있습니다.
# None이면 DEVDAY_MODEL 환경변수 또는 코드 기본값 gpt-6-luna 사용
MODEL = "gpt-6-luna"
