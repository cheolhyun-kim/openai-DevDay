# 선풍기 개념 검증 벤치마크

목표는 **같은 실험 자료에서 코드 변경 전후의 측정 가능률·판정·반복 실행 변동을 비교**하는 것이다. 실제 공정 설비의 위험도, 고장 진단 정확도, 조기 감지 시간을 검증하는 자료는 아직 아니다.

`python -m unittest discover -s tests -v`는 코드 오류를 검사한다. 여기의 벤치마크는 영상 사례를 실제 분석하고, API를 사용하는 모드에서는 반복 추론까지 검사한다. 두 검증을 모두 사용한다.

Windows에서 기존 테스트의 UTF-8 결과 읽기가 실패하면 `python -X utf8 -m unittest discover -s tests -v`로 실행한다. 이 프로젝트의 의존성을 설치한 Python 환경을 사용한다.

### 2026-10-09 업로드 자료의 실제 실행

이번 업로드는 미풍 정상·떨림 V1·떨림 V2·동전100원 각 7개, 총 28개 파일이다. 정상 첫 파일을 공통 기준으로 정상 나머지 6개와 실험 조건 21개를 비교하는 27개 개발 사례를 `fan_manifest.actual.json`에 선언했다. 중풍·강풍은 이번 자료에 없다. 파일 이름의 V1/V2를 임의로 고장 정도나 위험 순서로 해석하지 않는다.

이미 잘린 파일이므로 `register`로 원본 바이트와 VFR 타임스탬프를 그대로 복사했다. 이번 입력의 240fps는 인코더 메타데이터와 최소 프레임 시각 간격을 근거로 한 **잠정값**이다. 실제 촬영 FPS와 내보내기 중 프레임 보존은 아직 사용자 확인이 없다. 독립 촬영 여부도 확인되지 않아 전체 자료를 한 그룹의 `dev`로 두었다.

실행 결과와 원인 진단은 다음 파일에 저장한다.

- `results/fan-benchmark-20261009-01/baseline/report/report.html`: 전체 27회 결과와 조건별 그래프.
- `results/fan-benchmark-20261009-01/baseline/diagnostics/report.html`: 추적 중 손실과 밝기 필터 제외를 나눈 그래프·영역별 CSV.
- `results/fan-benchmark-20261009-01/runtime-check/`: 정상–정상 첫 실행. 기준과 대상을 모두 새로 측정.
- `results/fan-benchmark-20261009-01/dataset/dataset.json`: 앞으로도 재사용할 동일 입력 자료.

이번 27회 실행은 실제 실패한 정상 기준 측정을 저장해 재사용하고, 대상 27개는 각각 새로 측정하는 `measurement` 모드다. API 모델 성능과 같은 입력 반복 안정성은 평가하지 않았다. 정상 기준이 실패한 상태에서는 전체 보류가 나와도 대상 27개가 모두 나쁜 영상이라는 뜻은 아니다. 최초 기준 측정 비용을 제외한 실행시간을 서비스 전체 처리시간으로 사용하지 않는다.

측정 코드·ROI·FPS·대역·스레드 수를 수정하면 저장된 기준의 signature 검증이 실패하므로 새 기준을 측정해야 한다. `options.actual.frozen-reference.json`은 **이번 실행 기록 재현용**이다. 개선 버전은 캐시가 없는 설정으로 실행해 기준도 새로 측정한다.

```powershell
python -X utf8 -m devday.benchmark run --dataset results/fan-benchmark-20261009-01/dataset/dataset.json --options benchmarks/options.actual.measurement.json --revision improved-v1 --repeats 1 --opencv-threads 18 --out results/fan-improved-v1
python -X utf8 benchmarks/diagnose_tracking.py results/fan-improved-v1
python -X utf8 -m devday.benchmark compare results/fan-benchmark-20261009-01/baseline results/fan-improved-v1 --out results/fan-comparison-v1
```

최초 기준 측정의 재사용 여부가 달라지는 위 비교에서는 지연시간의 직접적인 개선율을 주장하지 않는다. 측정 가능률과 판정 보류 변화를 먼저 확인한다. 추적이 안정된 뒤 같은 사례의 반복 횟수를 고정해 `--repeats 5` 등으로 별도 기준/개선 실행을 비교한다.

## 1. 가진 자료로 무엇을 검증할 수 있는가

| 자료 | 기록할 실험 조건 | 기준 영상 |
|---|---|---|
| 약풍·중풍·강풍 | normal × weak/medium/strong | 같은 속도의 별도 정상 구간 |
| 약풍·중풍·강풍 덮개 제거1 | cover_removed_1 × 속도 | 같은 속도의 normal |
| 약풍·중풍·강풍 덮개 제거2 | cover_removed_2 × 속도 | 같은 속도의 normal |
| 약풍·중풍·강풍 동전 | coin × 속도 | 같은 속도의 normal |

덮개 제거1·2의 의미는 기록지에 따로 적는다. 임의로 위험 순서나 강도 순서를 부여하지 않는다. 약풍 정상과 강풍 실험을 비교해 속도 차이를 이상으로 평가하지 않는다.

`condition`은 실제 조작한 조건이다. `expected`는 평가 목표 라벨이다.

- `unchanged`: 같은 정상 조건의 서로 겹치지 않는 구간 또는 새 정상 촬영.
- `changed`: **사전에 정한 조작 조건을 찾아내는 과제**의 대상. 실제 진동 차이 정답이 있다면 센서/전문가/독립 계측 근거를 기록한다.
- `unknown`: 물리적인 차이를 확인하지 못한 자료. 점수·실행 성공·보류·재현성은 시각화하지만 정답률에는 넣지 않는다.

동전이나 덮개 제거가 반드시 진동 증가, 위험 증가 또는 고장을 뜻하지 않는다. `changed`를 조작 상태 라벨로 사용했다면 발표에도 **실험 조건 구분 성능**이라고 말한다. 특히 덮개를 제거하면 관측 부위나 가려짐 자체가 달라질 수 있으므로 `inconclusive`가 타당한 사례도 있다. ROI를 제안한 영상 정보만으로 조건이 드러날 수 있어, AI가 진동을 근거로 판단했는지도 별도 검토해야 한다.

## 2. 슬로모션은 재생 시간 대신 프레임을 기준으로 자른다

30초가 재생 시간인지 실제 촬영 시간인지 먼저 구분한다. 파일에 적힌 FPS를 실제 촬영 FPS로 복사하지 않는다.

```powershell
cd C:\Prometheus\openai-DevDay
python -m devday.benchmark probe "benchmarks\input\weak_normal.mov" "benchmarks\input\weak_coin.mov"
```

`probe`는 컨테이너 FPS, 프레임 수, 방향, 추정 재생 길이를 보여준다. 촬영 FPS나 원본 프레임 보존을 인증하지 않는다. 확인한 촬영 FPS를 알고 있다면 `--capture-fps 240`처럼 추가할 수 있지만, 모르면 촬영 기기의 설정과 내보내기 방식을 확인한 후 입력한다.

**연속 프레임이 보존되고 일정 간격으로 촬영된 경우에만** 실제 구간 길이 ≈ 프레임 수 / 촬영 FPS다. 예를 들어 240fps에서 720프레임은 3초, 120fps에서는 6초다. VFR/누락 프레임/슬로모션 편집 구간이 섞이면 이 계산과 물리적 Hz 해석이 틀릴 수 있다. FPS가 불확실한 자료로 물리적 주파수 정확도를 주장하지 않는다.

시작 설정은 실제 시간 기준 2–3초 클립으로 제안한다. 가장 낮은 관심 주파수가 1Hz라면 3초도 주기가 세 번뿐이므로, 별도로 더 긴 구간을 평가한다. 길이별 실험은 같은 데이터 분할 원칙을 유지하고 개발 자료에서 결정한다. 충분한 반복 주기가 없는 구간을 짧게 쪼개 사례 수만 늘리지 않는다.

원본 처음·끝의 손이나 조작, 속도 전환, 카메라 움직임이 들어간 구간은 기록하고 평가 목적에 따라 제외/품질 실패 사례로 분리한다. 분석 결과를 보고 불리한 구간만 제외하면 안 된다. 같은 원본에서는 클립이 겹치지 않도록 하고 필요하면 `minimum_gap_frames`로 간격을 둔다. 시간 간격을 둬도 독립적인 새 촬영이 되지는 않는다.

`prepare`는 원본을 수정하지 않고 새 파일을 만든다. FFmpeg가 PATH에 있으면 프레임 단위 `trim`과 timestamp passthrough를 이용해 PNG 코덱 MOV에 저장한다. 프레임 복제·삭제·보간을 요청하지 않고 microsecond timebase를 사용한다. FFmpeg가 없으면 **확인 가능한 일정 timestamp** 자료만 OpenCV/FFV1 AVI로 저장한다. VFR/불명확한 timestamp는 실패 처리하고 FFmpeg 사용을 안내한다. PNG/FFV1 자료는 용량이 크므로 작은 클립으로 먼저 확인한다.

클립마다 특징점을 새로 선택하고 추적한다. 전체 원본에서 끝까지 살아남은 추적점을 앞뒤로 나눈 기존 비교보다, 실제로 잘린 입력을 다시 분석하는 평가에 가깝다. 원본에 없던 프레임이나 누락된 촬영 정보를 복구하는 도구는 아니다.

## 3. 개발 자료와 평가 자료의 분리

**현재 조건별 원본 한 개씩이면, 여러 클립을 만들어도 독립 촬영 12회가 추가되지 않는다.** 같은 촬영 세션의 모든 원본에 같은 `group_id`를 부여한다. 같은 원본 및 같은 촬영 세션은 기준 영상까지 포함해 dev/test 양쪽에 걸치지 못하도록 검증한다.

현재 자료 전체를 `dev`로 두고 성능과 안정성을 탐색하는 것이 출발점이다. 최종 평가용으로 카메라와 선풍기를 다시 설치하고 같은 조건을 새로 촬영한다. 코드·프롬프트·ROI·주파수 대역·임계값·재시도 예산을 개발 자료에서 결정한 뒤 평가 자료를 연다. 이미 결과를 보며 여러 수정을 했다면 그 자료는 개발 자료다.

재촬영 전에는 원본 내 앞뒤 구간 비교 결과를 보고할 수 있지만, 독립 촬영에 대한 일반화 성능으로 설명하지 않는다. 반복 API 실행도 새로운 영상 표본이 아니다. 가능하면 속도별 정상과 조작 조건을 같은 세션에 짝지어 촬영하고, 정상 기준을 공유하는 사례들은 같은 그룹으로 유지한다. 그룹 수가 많아질수록 촬영 조건에 대한 검증 범위가 넓어진다.

도구의 세션 bootstrap 신뢰구간은 독립 세션 그룹이 5개 미만이면 표시하지 않는다. 5개는 충분한 표본 수를 보장하는 통계 기준이 아니라 작은 표본에서 확신을 과장하지 않기 위한 표시 제한이다. 5개 이상이어도 구간은 탐색적이며 세션 독립성과 대표성 가정이 필요하다.

## 4. 데이터 선언과 클립 생성

```powershell
python -m devday.benchmark init --out benchmarks/fan_manifest.json
```

12개 조건 템플릿이 만들어진다. 벤치마크 원본 영상은 `benchmarks/input/`에 넣는다. 원본 경로는 **manifest 파일 기준**이므로 `benchmarks/fan_manifest.json`에서는 `input/weak_normal.mov`처럼 적는다. `capture_fps`는 확인한 실제 촬영 값으로 채운다. 없는 자료의 recording은 삭제해도 된다. `frame_preservation_verified`는 확인한 경우에만 true다. 기본 템플릿은 FPS를 추정하지 않기 위해 null이고 클립/사례가 비어 있다.

다음은 한 속도의 설정 예시다. 숫자는 사용법 예시이며 실제 프레임 수와 확인한 FPS에 맞춰 변경한다. ROI 경로는 각 영상 쌍에서 검토한 정규화 좌표 계획이다.

```json
{
  "schema_version": "devday.dataset/1",
  "minimum_gap_frames": 120,
  "recordings": [
    {"id":"s0","path":"input/weak_normal.mov","speed":"weak","condition":"normal","group_id":"session01","capture_fps":240,"frame_preservation_verified":false},
    {"id":"s1","path":"input/weak_coin.mov","speed":"weak","condition":"coin","group_id":"session01","capture_fps":240,"frame_preservation_verified":false}
  ],
  "clips": [
    {"id":"c0","recording_id":"s0","start_frame":120,"end_frame":840},
    {"id":"c1","recording_id":"s0","start_frame":960,"end_frame":1680},
    {"id":"c2","recording_id":"s1","start_frame":120,"end_frame":840}
  ],
  "cases": [
    {"id":"case01","reference_clip":"c0","candidate_clip":"c1","split":"dev","expected":"unchanged","roi_plan_path":"roi/normal-normal.json"},
    {"id":"case02","reference_clip":"c0","candidate_clip":"c2","split":"dev","expected":"unknown","roi_plan_path":"roi/normal-coin.json"}
  ]
}
```

프레임 범위는 `[start_frame, end_frame)`이다. 같은 클립을 여러 비교의 기준으로 재사용할 수 있으나 사례들이 독립 표본이 되지는 않는다. 같은 클립 자신과의 비교는 지나치게 쉬운 대조군이므로 거부한다.

```powershell
python -m devday.benchmark prepare --manifest benchmarks/fan_manifest.json --out results/fan-dataset-v1
```

`dataset.json`과 무손실 클립을 생성하고 원본/클립 SHA-256을 기록한다. 분석 입력 파일명은 `c00000`처럼 바꾼다. runner는 라벨이나 조건 이름을 adapter에 전달하지 않는다. API에 영상상 상태가 보이는 것은 막지 못한다.

이미 자른 영상 파일을 사용할 때는 각 recording에 대해 전체 파일 범위 `[0, frame_count)`를 clip으로 선언하고 다음 명령을 사용한다. 파일 내용을 그대로 복사하므로 VFR timestamp를 유지하고 FFmpeg가 필요 없다. 부분 구간은 `prepare`로 처리한다.

```powershell
python -m devday.benchmark register --manifest benchmarks/fan_manifest.json --out results/fan-dataset-v1
```

## 5. 세 가지 평가와 반복 실행

| 모드 | 실행 내용 | 확인하려는 문제 |
|---|---|---|
| `measurement` | 검토한 고정 ROI → 정합/추적/측정/규칙. API 호출 없음 | 영상 측정과 규칙의 분리 성능·실패·속도 |
| `fixed-roi` | 고정 ROI → 측정 → AI 해석 → 최종 서비스 판정 | 동일 측정 조건에서 AI 해석 변동 |
| `full` | AI ROI 제안 → 측정 → AI 해석 → 최종 서비스 판정 | 전체 서비스의 성공률과 변동 |

`measurement`도 원본 클립을 매번 다시 분석한다. 결정적인 코드라면 같은 입력의 반복 결과가 같을 수 있다. 그 반복의 의미는 API 무작위성 검사가 아니며, 실제 새 촬영 클립들 사이의 차이도 함께 봐야 한다. `fixed-roi`는 ROI가 고정되어도 매번 측정을 실행하고, `full`은 매번 새 ROI 추론을 실행한다. SDK/네트워크/배경 추적/응답 검증 실패를 구분하려면 각 `worker.log`와 application artifact를 확인한다.

측정 옵션의 `reference_cache_path`는 명시적으로 만든 `devday.frozen_reference/1` 정상 기준 측정 파일을 재사용하는 선택 기능이다. 원본 SHA-256·ROI/config·대역·측정 코드·OpenCV 스레드 수의 signature를 검증하며, 일치하지 않으면 실패한다. 이 실행에서는 대상 영상만 새로 측정하고, 보고서에 기준 측정 재사용과 지연시간 범위를 표시한다. 기준 측정 실패를 재사용하면 그 실패도 모든 사례의 공통 보류 원인으로 남는다. 독립 촬영이나 정상 기준 재측정의 반복 안정성을 검증하는 기능은 아니다.

```powershell
python -m devday.benchmark run --dataset results/fan-dataset-v1/dataset.json --options benchmarks/options.measurement.json --revision baseline --repeats 5 --out results/bench-baseline
```

사례당 ROI가 없다면 options JSON의 `roi_plan_path`로 공통 계획을 지정할 수 있다. options 내 경로는 실행 디렉터리 기준이다. 실제 촬영 조건을 기록하고 분석에 사용한 FPS와 배경 추적 품질을 확인한다. 저장된 ROI가 시작 구간마다 적절한지는 먼저 점검한다.

전체 API 평가는 options.full.json을 복사해 사용할 모델과 확인된 촬영 조건을 명시한 후 실행한다. OpenAI API 키는 프로젝트 루트의 `settings.py`에 `OPENAI_API_KEY = "본인 키"`로 입력하거나 환경변수로 제공한다. `settings.py`의 키가 채워져 있으면 해당 키를 우선 사용한다. `settings.py`는 Git에서 제외되며 키를 benchmark options JSON에 넣지 않는다. `full`과 `fixed-roi` 모드에서 실제 외부 호출과 비용이 발생한다. 미완료/거부 응답을 성공으로 대체하지 않는다. 코드에 있는 재시도를 그대로 실행하고 최종 실패도 집계한다.

```powershell
python -m devday.benchmark run --dataset results/fan-dataset-v1/dataset.json --options benchmarks/options.full.json --revision full-v1 --repeats 10 --out results/bench-full-v1
```

처음에는 클립/속도/조건별로 적은 반복으로 연결을 점검하고, 최종 반복 수는 결과를 보기 전에 고정한다. full 10회는 개발용 시작 제안이며 안정성의 통계적 보장을 뜻하지 않는다. 결과가 나쁜 경우에만 추가 실행해서 좋은 결과를 선택하지 않는다. 전체 사례를 같은 횟수로 반복한다.

매 시도는 새 subprocess·새 출력 폴더에서 실행된다. 실행 순서는 seed로 섞으며 같은 revision 비교는 동일 사례·repeat·seed를 짝짓는다. Python/NumPy seed는 기록하지만 API 결과를 결정적으로 만들지는 않는다. 모든 시도를 `trials.jsonl`에 기록한다. timeout이나 crash 이후에도 다음 사례를 실행한다. 중단되어 계획한 시도가 빠진 자료는 `summarize`와 `compare`가 거부한다.

`--opencv-threads 1`처럼 OpenCV 스레드 수를 고정할 수 있다. 이 값은 실행 정보에 기록하고 ROI·추적 품질·판정 임계값을 바꾸지 않는다. 기본값은 설치 환경의 OpenCV 설정이다. 지연시간 비교에는 같은 스레드 수와 같은 실행 환경을 사용한다.

## 6. 지표 해석과 시각화

`report/report.html`에서 PNG 그래프를 보고 SVG를 발표 자료로 내보낼 수 있다. CSV는 모든 시도의 라벨·오류·점수·지연시간과 artifact 경로를 포함한다.

| 지표/그래프 | 의미 |
|---|---|
| completion_rate | 오류 없이 끝난 비율. 판단 보류도 실행 완료에는 포함 |
| measurement_coverage | 유효 측정 영역이 하나 이상 있는 시도 / 모든 시도 |
| decision_coverage | 변경/유지 결론을 낸 시도 / 모든 시도 |
| effective_correctness | 정답인 시도 / 라벨 있는 모든 시도. 보류·실패 포함 |
| conditional_accuracy | 정답인 시도 / 실제로 결론을 낸 라벨 있는 시도. 단독으로 제시하지 않음 |
| end_to_end_changed_hit_rate | changed 라벨 중 변경 판정 비율. 보류·실패도 분모에 포함 |
| false_alarm_rate_all_unchanged | unchanged 라벨 중 변경 판정 비율. coverage와 함께 읽음 |
| abstention_rate / failure_rate | 근거 부족 보류와 실행 오류를 분리 |
| repeatability.png | 사례 × 반복 결과. 매번 실패하는 결과도 색으로 보임 |
| conditions.png | 속도·실험 조건별 결과 분포. 평균에 가려진 약한 조건을 확인 |
| confusion.png | 변경/유지/보류/실패의 전체 건수. unknown은 정답률 제외 |
| scores.png | 각 시도의 변경 점수. 측정 누락은 다른 지표에 포함 |

서비스 최종 decision이 존재하면 그것을 우선한다. AI의 assessment가 이상이어도 최종 decision이 insufficient면 보류다. measurement 모드는 같은 규칙 사전판정을 출력하므로 AI 성능으로 보고하지 않는다.

기본 점수는 해석 가능한 target 영역의 `max(abs(log2(candidate_rms/reference_rms)))`이다. 예: 2배 또는 0.5배는 점수 1이다. 증가와 감소를 모두 보여주기 위한 탐색 지표로, 위험 확률이 아니다. ROI/대역 개수가 늘수록 최대값이 커질 수 있고, 영역 선택에 따라 값이 변하므로 조건을 고정해 비교한다. 기존 evidence v1과 v2는 RMS 정의가 다르므로 서로의 절대 점수를 동일 계측값으로 취급하지 않는다.

`--threshold`를 명시하면 서비스 판정 대신 점수 기준으로 구분 성능을 평가한다. 개발 자료에서만 정한 값을 모든 버전/평가 자료에 그대로 사용한다. 서비스 판단과 별도 점수의 성능을 섞지 않는다. 임계값을 test 결과를 보며 바꾸면 그 결과는 탐색 결과다. 보고서에 판정 기준과 임계값을 표시한다.

## 7. 코드 개선 전후 비교

기존 데이터를 다시 자르지 않고 같은 `dataset.json`을 사용한다. ROI 선택, 배경 보정, 특징점 생존 조건, 잡음 기준, 프롬프트 등을 **한 번에 하나씩** 수정해 원인을 이해한다. 코드 수정 중에는 benchmark를 돌리지 않는다. 모델 종류/버전·수정된 prompt·ROI 계획·threshold·라이브러리도 실험 설정의 일부다.

```powershell
# 코드 개선 후, 같은 options/data/seed/repeats를 사용
python -m devday.benchmark run --dataset results/fan-dataset-v1/dataset.json --options benchmarks/options.measurement.json --revision improved --repeats 5 --out results/bench-improved
python -m devday.benchmark compare results/bench-baseline results/bench-improved --out results/bench-comparison
```

compare는 데이터 ID·분할·모든 사례/반복/seed가 같아야 실행된다. 실패를 빼고 비교할 수 없다. 각 버전의 전체 기준 지표, 같은 시도의 정답률 차이, 세션 단위 paired bootstrap 구간을 저장한다. CLI에서 점수 threshold를 평가했다면 compare에도 같은 `--threshold`를 넣는다.

측정/추적 개선을 확인한 뒤 fixed-roi/full을 비교하면 변동 원인을 좁힐 수 있다. 다른 모델의 라이브 추론을 서로 다른 시간에 실행하는 비교에는 외부 서비스/네트워크/시간 변동도 섞인다. 기록만으로 이를 완전히 통제하지는 못한다.

## 8. 분석 코드가 바뀌어도 유지하는 연결 규약

`data.py`는 입력 자료, `runner.py`는 반복 실행, `metrics.py`는 지표, `report.py`는 그림만 맡는다. 애플리케이션 함수·JSON 구조를 아는 부분은 `adapters.py`다. 미래 코드가 API 함수나 evidence schema를 바꾸면 adapter를 수정/교체한다. **모든 미래 코드와 자동 호환을 보장하는 방식은 아니다.** 새 스키마를 조용히 잘못 해석하는 대신 adapter 오류로 기록한다.

다른 분석기를 연결하려면 import 가능한 Python module:function을 제공한다.

```python
# my_adapter.py (프로젝트 루트)
def evaluate(request):
    # request: schema_version, case_id, seed, reference_video,
    # candidate_video, capture_fps, candidate_capture_fps,
    # output_dir, options, roi_plan_path. 평가 라벨은 전달하지 않는다.
    result = my_analysis(request["reference_video"], request["candidate_video"])
    return {
        "schema_version": "devday.observation/1",
        "decision": result["final_label"],  # abnormal/normal/insufficient 또는 null
        "assessment": None,  # 선택: suspected_abnormal/no_clear_difference/inconclusive
        "score": result.get("change_score"),  # 유한한 0 이상 숫자 또는 null
        "measurement_available": result["has_valid_evidence"]  # bool
    }
```

```powershell
python -m devday.benchmark run --dataset results/fan-dataset-v1/dataset.json --adapter my_adapter:evaluate --revision custom-v1 --repeats 5 --out results/bench-custom-v1
```

앱 코드/프롬프트 hash, adapter hash, Git commit 및 dirty 여부, Python 환경, 모델 응답/사용량, ROI hash, 전체 schedule을 저장한다. credentials를 options에 넣는 것은 거부한다. 사용자 정의 adapter가 별도 하위 프로세스를 띄우면 해당 프로세스의 종료와 자원 정리는 adapter 책임이다.

새 score의 정의가 기존 score와 다르면 같은 threshold를 공유하지 말고 서비스 판정의 지표를 비교한다. 반환 규약을 유지하면 지표/시각화는 계속 사용할 수 있다.

## 9. API 없이 도구 확인

```powershell
python -m devday.benchmark demo --out results/benchmark-demo
python -m unittest discover -s tests -p test_benchmark.py -v
```

demo는 알려진 23.4Hz 움직임 합성 영상에서 정상–정상/정상–움직임 증가 사례를 자르고 각각 두 번 실제 추적한다. API 호출은 없고 분석 도구 연결 검증용이다. 여기서의 100%도 실제 팬/AI/공정 설비의 정확도가 아니다.

## 10. 발표에서 사용할 문장

결과를 채운 뒤 다음 형식으로 말한다.

> “현재 선풍기 실험 자료에서 N개 비교 사례를 각각 R번 실행했습니다. 전체 M회 중 분석 완료는 A회, 유효 측정은 B회, 판정 보류는 C회, 실행 실패는 D회였습니다. 라벨을 정한 조작 조건에 대한 탐지율과 정상 조건의 오탐률은 다음과 같고, 같은 입력의 반복 판단 변동도 함께 공개합니다. 이 결과는 현재 자료의 개념 검증이며 실제 설비의 고장이나 조기 위험 감지는 추가 검증이 필요합니다.”

독립 평가 세션을 확보한 후에는 개발 자료와 구분해 촬영 세션 수와 결과를 함께 제시한다. 실제 조기 감지 주장을 위해서는 고장/상태 전환 시점과 영상 시계열, 센서 및 전문가 판정이 필요하다. 현재처럼 상태별 완성 영상만 있으면 조기 감지 지연은 측정할 수 없다.

라이브 데모가 실패하면 **같은 버전으로 미리 저장한 전체 benchmark 보고서**, 특정 실패의 로그·보류 원인, 저장 결과임을 밝힌 시연을 제시한다. 성공한 일부 실행만 골라 서비스 성능을 대표시키지 않는다.

그 다음 검증 항목은: 정답 ROI annotation에 대한 위치 일치도, 근거 없는/과도한 고장 주장, 전문가 점검 후보·행동과의 일치도, 재촬영 필요 판단의 적절성이다. 현재 자동 지표는 이 의미적 품질을 입증하지 않는다.

참고: 원본/세션별 분할 원칙은 [scikit-learn의 그룹 교차검증 설명](https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data), 개발 자료에서 설정을 선택하는 원칙은 [데이터 누수 설명](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage)에 따른다. [FFmpeg trim](https://ffmpeg.org/ffmpeg-filters.html#trim)과 [timestamp passthrough](https://ffmpeg.org/ffmpeg.html#Advanced-options)의 동작을 사용한다.
