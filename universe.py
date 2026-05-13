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

KR_UNIVERSE_SIZE = 200
KR_MIN_MARKET_CAP_KRW = 100_000_000_000  # 1,000억

HISTORY_DAYS = 400               # calendar days of history to fetch (~270 trading days)
ROLLING_WINDOW_DAYS = 250        # trading days for beta estimation
US_MOVE_THRESHOLD_PCT = 1.5      # ignore US moves smaller than this in scoring
MIN_VOLUME_RATIO = 1.5           # require recent vol >= 1.5x 20-day median
TOP_K_PICKS = 3
TOP_K_DRIVERS_PER_STOCK = 3      # use top-3 most-correlated US drivers per KR stock
