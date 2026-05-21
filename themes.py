"""Narrow theme dictionary: US proxy → KR mapping.

Used by theme_scanner to detect overnight US theme strength and surface
corresponding KR theme stocks (more granular than the broad sector ETFs in
universe.py). Themes here are intentionally narrow (양자/SMR/휴머노이드/비만 등)
— the kind of themes that move 한국 single-name 종목들 but get drowned out in
broad SMH/XLK/XLV signals.

Each theme:
  label:      Display string used in the Telegram message
  us_proxies: US tickers that move together on this theme (overnight signal)
  kr_stocks:  (ticker6, name) list — Korean stocks commonly mapped to this theme

KR mappings are best-effort and need periodic review (테마 변동성 큼). User-editable.
"""

THEMES = {
    "quantum": {
        "label": "⚛️ 양자컴퓨팅",
        "us_proxies": ["IONQ", "RGTI", "QBTS", "ARQQ", "QTUM"],
        "kr_stocks": [
            ("032820", "우리기술"),
            ("115500", "케이씨에스"),
            ("056360", "코위버"),
            ("050890", "쏠리드"),
            ("054450", "텔레칩스"),
            ("025770", "한국정보통신"),
        ],
    },
    "smr_nuclear": {
        "label": "☢️ SMR·소형원전",
        "us_proxies": ["SMR", "OKLO", "NNE", "LEU"],
        "kr_stocks": [
            ("034020", "두산에너빌리티"),
            ("052690", "한전기술"),
            ("036640", "HRS"),
            ("083650", "비에이치아이"),
            ("105840", "우진"),
        ],
    },
    "humanoid": {
        "label": "🤖 휴머노이드로봇",
        "us_proxies": ["SYM", "BOTZ", "RBOT", "IRBT"],
        "kr_stocks": [
            ("277810", "레인보우로보틱스"),
            ("454910", "두산로보틱스"),
            ("090360", "로보스타"),
            ("108490", "로보티즈"),
            ("058610", "에스피지"),
            ("140670", "알에스오토메이션"),
        ],
    },
    "space": {
        "label": "🚀 우주항공",
        "us_proxies": ["RKLB", "ASTS", "LUNR", "ASTR", "IRDM"],
        "kr_stocks": [
            ("047810", "한국항공우주"),
            ("099320", "쎄트렉아이"),
            ("189300", "인텔리안테크"),
            ("274090", "켄코아에어로스페이스"),
            ("211270", "AP위성"),
        ],
    },
    "obesity": {
        "label": "💉 비만치료제(GLP-1)",
        "us_proxies": ["LLY", "NVO", "VKTX", "ALT"],
        "kr_stocks": [
            ("128940", "한미약품"),
            ("087010", "펩트론"),
            ("347850", "디앤디파마텍"),
            ("389470", "인벤티지랩"),
            ("226950", "올릭스"),
        ],
    },
    "autonomous": {
        "label": "🚗 자율주행·Robotaxi",
        "us_proxies": ["MBLY", "AUR", "TSLA"],
        "kr_stocks": [
            ("118990", "모트렉스"),
            ("089010", "켐트로닉스"),
            ("204320", "HL만도"),
            ("092200", "디아이씨"),
            ("317120", "라닉스"),
        ],
    },
    "cybersecurity": {
        "label": "🛡️ 사이버보안",
        "us_proxies": ["CRWD", "PANW", "ZS", "S", "NET", "FTNT"],
        "kr_stocks": [
            ("053800", "안랩"),
            ("136540", "윈스"),
            ("131090", "시큐브"),
            ("042510", "라온시큐어"),
            ("053350", "이니텍"),
            ("170790", "파이오링크"),
        ],
    },
    "data_center_power": {
        "label": "⚡ 데이터센터전력",
        "us_proxies": ["VRT", "ETN", "MOD", "GEV", "NEE"],
        "kr_stocks": [
            ("010120", "LS ELECTRIC"),
            ("103590", "일진전기"),
            ("000500", "가온전선"),
            ("298040", "효성중공업"),
            ("006340", "대원전선"),
        ],
    },
    "defense_narrow": {
        "label": "🪖 방산",
        "us_proxies": ["RTX", "LMT", "NOC", "GD", "BWXT", "LDOS", "KTOS"],
        "kr_stocks": [
            ("012450", "한화에어로스페이스"),
            ("272210", "한화시스템"),
            ("079550", "LIG넥스원"),
            ("103140", "풍산"),
            ("005870", "휴니드"),
        ],
    },
}


def all_us_proxies() -> list:
    """Unique flat list of all US proxy tickers across themes (for batch fetch)."""
    seen = set()
    out = []
    for theme in THEMES.values():
        for sym in theme["us_proxies"]:
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out
