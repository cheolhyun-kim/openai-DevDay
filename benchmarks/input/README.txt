벤치마크용 원본 영상을 이 폴더에 넣으세요.

먼저 약풍 정상과 약풍 동전 두 개로 시작할 수 있습니다.

조건별 권장 파일명 (MOV 원본인 경우):
  약풍 정상         weak_normal.mov
  약풍 덮개 제거1   weak_cover_removed_1.mov
  약풍 덮개 제거2   weak_cover_removed_2.mov
  약풍 동전         weak_coin.mov
  중풍 정상         medium_normal.mov
  중풍 덮개 제거1   medium_cover_removed_1.mov
  중풍 덮개 제거2   medium_cover_removed_2.mov
  중풍 동전         medium_coin.mov
  강풍 정상         strong_normal.mov
  강풍 덮개 제거1   strong_cover_removed_1.mov
  강풍 덮개 제거2   strong_cover_removed_2.mov
  강풍 동전         strong_coin.mov

원래 파일명을 사용해도 됩니다. benchmarks/fan_manifest.json의 path를 맞추세요.
MP4 등 다른 형식이면 실제 확장자를 유지하고 manifest 경로를 수정하세요.

영상과 함께 확인할 내용:
  - 실제 촬영 FPS (120/240 등, 모르면 모름)
  - 원본인지 편집/내보내기한 파일인지
  - 촬영 세션, 속도, 조작 조건, 카메라 고정 여부
  - 덮개 제거1과 제거2의 차이

fan_manifest.example.json을 fan_manifest.json으로 복사한 뒤 작성하세요.
촬영 FPS와 클립 구간이 정해지면 prepare로 잘린 데이터를 생성합니다.
생성되는 클립과 분석 결과는 results/에 저장됩니다.
이 폴더의 영상은 Git에서 제외됩니다.

자세한 실행 방법: benchmarks/README.md
