"""Asymmetric Bet Universe — small-cap candidates in secular-trend themes.

Different game from Must-Buy (Buffett quality). This is venture-style: 90% will
fail or drift, but a single 50-100x return in 5 years can pay for the rest.
Mandatory: diversify across 10+ names, hold 5 years, expect ride to be brutal.

Selection criteria for inclusion:
- Operates in a theme with TAM expanding 5-10x over the next decade
- Market cap small enough that 10-50x is mathematically possible (~$1-30B)
- Public, US-listed (KR small-caps deserve a separate file due to data gaps)

This list is curated, not exhaustive. Refresh manually as themes evolve.
"""

ASYMMETRIC_THEMES = {
    "quantum": {
        "label": "⚛️ 양자컴퓨팅",
        "thesis": "양자우월성 달성 시 IT 패러다임 전환. 첫 상업적 활용 사례가 나오는 종목이 10-100x 가능. 90%는 hype로 끝남.",
        "tickers": [
            ("IONQ", "IonQ"),
            ("RGTI", "Rigetti Computing"),
            ("QBTS", "D-Wave Quantum"),
            ("ARQQ", "Arqit Quantum"),
            ("QUBT", "Quantum Computing Inc"),
            ("QSI", "Quantum-Si"),
        ],
    },
    "smr_nuclear": {
        "label": "☢️ SMR·소형원전",
        "thesis": "AI 데이터센터 전력 폭증 + 탄소중립 → 30년 stagnant 원전 산업 재개. SMR 첫 상업운전 + 후속 수주 시 30-50x.",
        "tickers": [
            ("OKLO", "Oklo"),
            ("SMR", "NuScale Power"),
            ("NNE", "Nano Nuclear Energy"),
            ("LEU", "Centrus Energy"),
            ("ASPI", "ASP Isotopes"),
        ],
    },
    "humanoid_robotics": {
        "label": "🤖 휴머노이드·로봇",
        "thesis": "노동력 부족 + AI = 10년 내 휴머노이드 양산. 메이저는 비상장(Figure, 1X) — 상장 픽스 제한적.",
        "tickers": [
            ("SYM", "Symbotic"),
            ("RBOT", "Vicarious Surgical"),
            ("PATH", "UiPath"),
            ("BOTZ", "Global X Robotics & AI ETF"),
        ],
    },
    "space_economy": {
        "label": "🚀 우주경제",
        "thesis": "Starlink·Kuiper·6G 통신 인프라 재편. 위성 인터넷·우주제조·달자원 단계별. 첫 흑자 종목 10-30x.",
        "tickers": [
            ("RKLB", "Rocket Lab"),
            ("ASTS", "AST SpaceMobile"),
            ("LUNR", "Intuitive Machines"),
            ("PL", "Planet Labs"),
            ("IRDM", "Iridium Communications"),
            ("RDW", "Redwire"),
        ],
    },
    "gene_therapy_crispr": {
        "label": "🧬 유전자치료·CRISPR",
        "thesis": "단발 치료로 난치병 정복. 단일 약물 승인 시 20-100x. 임상 실패 시 -80%. 분산 필수.",
        "tickers": [
            ("NTLA", "Intellia Therapeutics"),
            ("BEAM", "Beam Therapeutics"),
            ("CRSP", "CRISPR Therapeutics"),
            ("EDIT", "Editas Medicine"),
            ("VERV", "Verve Therapeutics"),
            ("PRME", "Prime Medicine"),
        ],
    },
    "obesity_next_gen": {
        "label": "💉 비만·차세대 GLP-1",
        "thesis": "Lilly/NVO는 이미 큼. 차세대(경구·다중작용·근손실 방지) 개발 중인 소형주가 인수/승인 시 큰 비대칭.",
        "tickers": [
            ("VKTX", "Viking Therapeutics"),
            ("ALT", "Altimmune"),
            ("STRC", "Sutro Biopharma"),
            ("ZEAL", "Zealand Pharma"),
        ],
    },
    "battery_energy_storage": {
        "label": "🔋 배터리·에너지저장",
        "thesis": "그리드 수준 ESS + 차세대 배터리 화학. 양산 진입 + 단가 하락 시 비대칭. 90%는 capex 못 견디고 사라짐.",
        "tickers": [
            ("STEM", "Stem Inc"),
            ("FLNC", "Fluence Energy"),
            ("ENVX", "Enovix"),
            ("QS", "QuantumScape"),
            ("SLDP", "Solid Power"),
        ],
    },
    "synthetic_biology": {
        "label": "🧪 합성생물학",
        "thesis": "DNA를 소프트웨어처럼 디자인. 의약·재료·식품 다 재편 가능. 아직 'iPhone moment' 전 단계.",
        "tickers": [
            ("DNA", "Ginkgo Bioworks"),
            ("CDNA", "CareDx"),
            ("PACB", "Pacific Biosciences"),
            ("TWST", "Twist Bioscience"),
        ],
    },
    "ai_infra_picks": {
        "label": "⚡ AI 인프라 picks-and-shovels",
        "thesis": "NVDA는 이미 큼. 데이터센터 전력·냉각·전송에서 시총 작은 second-derivative 후보.",
        "tickers": [
            ("MOD", "Modine Manufacturing"),
            ("AEHR", "Aehr Test Systems"),
            ("PWR", "Quanta Services"),
            ("VRT", "Vertiv Holdings"),
            ("BE", "Bloom Energy"),
        ],
    },
    "post_quantum_security": {
        "label": "🔐 양자내성암호 (PQC)",
        "thesis": "양자컴 발달 시 RSA·ECC 깨짐 → 전 세계 암호 인프라 재구축 필요. 표준화 진행 중인 새 시장.",
        "tickers": [
            ("ARQQ", "Arqit Quantum"),
            ("S", "SentinelOne"),
        ],
    },
}


def all_tickers() -> list:
    """Flat unique list of all asymmetric tickers (for batch fetch)."""
    seen = set()
    out = []
    for theme in ASYMMETRIC_THEMES.values():
        for sym, name in theme["tickers"]:
            if sym not in seen:
                seen.add(sym)
                out.append((sym, name))
    return out
