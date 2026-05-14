"""Define the universe of US 'drivers' and Korean stock candidates."""

US_DRIVERS = [
    # Mega cap individual stocks
    "NVDA", "TSLA", "TSM", "SMCI", "AAPL", "MSFT",
    "AMD", "MU", "META", "GOOGL", "AMZN", "AVGO", "NFLX",
    # Sector ETFs
    "SMH",   # semiconductors
    "XLK",   # tech
    "XLE",   # energy
    "XLV",   # healthcare
    "XLF",   # financials
    "XLI",   # industrials
    "XLB",   # materials
    "XLY",   # consumer discretionary
    "URA",   # uranium / nuclear
    "ITA",   # defense aerospace
    "XBI",   # biotech
    "KWEB",  # china internet
    "EWY",   # MSCI Korea ETF
]

KR_UNIVERSE_SIZE = None          # None = no cap; take everything above KR_MIN_MARKET_CAP_KRW
KR_MIN_MARKET_CAP_KRW = 100_000_000_000  # 1,000억 (user preference; Tier 1 filters handle noise)

HISTORY_DAYS = 600               # ~400 trading days — enough for walk-forward backtest with 250-day train window
ROLLING_WINDOW_DAYS = 250        # trading days for beta estimation
US_MOVE_THRESHOLD_PCT = 1.5      # ignore US moves smaller than this in scoring
MIN_VOLUME_RATIO = 1.5           # require recent vol >= 1.5x 20-day median
MAX_VOLUME_RATIO_CAP = 10.0      # exclude event-driven outliers (>10x normal volume = abnormal event)
TOP_K_PICKS = 3
TOP_K_DRIVERS_PER_STOCK = 3      # use top-3 most-correlated US drivers per KR stock

# Tier 1 filters
MIN_CORR_THRESHOLD = 0.15        # ignore (US driver, KR stock) pairs with weaker relationship
MIN_OBSERVATIONS = 120           # require ≥ 120 trading days of paired returns
MAX_ABS_BETA = 3.0               # cap |β| to filter spurious correlations (e.g. small-cap × big driver = β=-9 noise)
TRANSACTION_COST_PCT = 0.5       # round-trip slippage + fees + tax for KR day-trading
SECTOR_DIVERSIFY = True          # don't allow 2 picks sharing the same primary US driver

# Korean market broad index (used for regime detection — strong EWY move = risk-off signal)
KR_MARKET_PROXY = "EWY"
RISK_OFF_THRESHOLD_PCT = 3.0     # if |EWY move| >= this, flag a regime warning

# Sector grouping for display: list of {name, etf, stocks}
SECTORS = [
    {"name": "반도체",         "etf": "SMH",  "stocks": ["NVDA", "TSM", "AMD", "MU", "SMCI", "AVGO"]},
    {"name": "빅테크/IT",       "etf": "XLK",  "stocks": ["AAPL", "MSFT", "META", "GOOGL", "AMZN", "NFLX"]},
    {"name": "전기차/EV",       "etf": None,   "stocks": ["TSLA"]},
    {"name": "헬스케어",        "etf": "XLV",  "stocks": []},
    {"name": "바이오",          "etf": "XBI",  "stocks": []},
    {"name": "에너지",          "etf": "XLE",  "stocks": []},
    {"name": "원전/우라늄",     "etf": "URA",  "stocks": []},
    {"name": "방산",            "etf": "ITA",  "stocks": []},
    {"name": "금융",            "etf": "XLF",  "stocks": []},
    {"name": "산업재",          "etf": "XLI",  "stocks": []},
    {"name": "소재",            "etf": "XLB",  "stocks": []},
    {"name": "경기소비재",      "etf": "XLY",  "stocks": []},
    {"name": "중국인터넷",      "etf": "KWEB", "stocks": []},
    {"name": "🇰🇷 한국 ETF (시장 프록시)", "etf": "EWY", "stocks": []},
]

FINVIZ_SECTOR_URL = "https://finviz.com/groups.ashx?g=sector&v=210&o=name"
FINVIZ_MAP_URL = "https://finviz.com/map"
