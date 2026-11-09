# Game registry. Every source here was tested from the box on 2026-11-09 (KST).
GAMES = [
 dict(id="starrail", name="붕괴: 스타레일", trends="붕괴 스타레일", yt="UCH33CJMcI0XZUpIhWRHiUuw", arca="hkstarrail",
      pickup=dict(kind="hoyo_ann", game="hkrpg")),
 dict(id="genshin", name="원신", trends="원신", yt="UCcum1rCJ5GJeQ_xv0xrohqg", arca="genshin",
      pickup=dict(kind="hoyo_ann", game="hk4e")),
 dict(id="zzz", name="젠레스 존 제로", trends="젠레스 존 제로", yt="UCmry1hfaRHI_iTfxUMhC8mA", arca="zenlesszonezero",
      pickup=dict(kind="hoyolab_news", gid=8)),
 dict(id="wuwa", name="명조: 워더링 웨이브", trends="명조", yt="UCKuq0c-RXYaulECSuu5hFug", arca="wutheringwaves",
      pickup=dict(kind="naver_lounge", lounge="WutheringWaves")),
 dict(id="nikke", name="승리의 여신: 니케", trends="니케", yt="UCzoVPjV8FF2om0Zj3XRPwVw", arca="nikketgv",
      pickup=dict(kind="naver_lounge", lounge="nikke")),
 dict(id="trickcal", name="트릭컬 리바이브", trends="트릭컬", yt="UCI37oS0O5OlmQNeiyIywEAQ", arca="trickcal",
      pickup=dict(kind="naver_lounge", lounge="trickcal")),
 dict(id="nte", name="이환", trends="이환 게임", yt="UC0042GfGERAl0AzXR0j8TPA", arca="nevernesstoeverness",
      pickup=dict(kind="naver_lounge", lounge="NTE")),
 dict(id="endfield", name="명일방주: 엔드필드", trends="엔드필드", yt="UCIf9gYpmmIJV7flBhUhuqYQ", arca="endfield",
      pickup=dict(kind="naver_lounge", lounge="Arknights_Endfield")),
 dict(id="priconne", name="프린세스 커넥트! Re:Dive", trends="프리코네", yt="UCF8zvIqu8LyXVfo6yHyqShA", arca="priconne",
      pickup=dict(kind="none", note="KR 공식 공지 소스 미확인. JP 공식 X(@priconne_redive)/게임 내 공지뿐 -> 데이터 없음")),
 dict(id="bangdream", name="뱅드림! 걸즈 밴드 파티!", trends="뱅드림", yt="UCN-bFIdJM0gQlgX7h6LKcZA", arca="bangdream",
      pickup=dict(kind="wp_json", base="https://bang-dream.bushimo.jp")),
]
WEIGHTS = dict(youtube=0.30, x=0.25, community=0.20, search=0.15, momentum=0.10)
