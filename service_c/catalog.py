"""Global Multi-Exchange Asset Catalog & Curated News Corpus for Service C.

Covers:
1. Exchange Crypto Pairs (LAME Spot: BTC-USDT, ETH-USDT, SOL-USDT, BNB-USDT, XRP-USDT, DOGE-USDT, ADA-USDT, AVAX-USDT)
2. US Equities — NASDAQ & NYSE Top 10 (NVDA, AAPL, MSFT, AMZN, META, TSLA, GOOGL, AVGO, BRK.B, JPM, AMD, LLY)
3. European Equities — LSE, Euronext, Xetra Top 10 (ASML, LVMH, NVO, SAP, SHEL, AZN, HSBA, SIE, TTE, ULVR)
4. Asia-Pacific Equities — TSE, HKEX, TWSE, KRX Top 10 (TSM, 0700.HK/TCEHY, 9988.HK/BABA, 7203.T/TM, 005930.KS/SAMSUNG, 6758.T/SONY, 1211.HK/BYD, 8306.T/MUFG, 3690.HK/MEITUAN, 9984.T/SFTBY)
5. Indian Equities — NSE & BSE Top 10 (RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, BHARTIARTL, SBIN, LT, ITC, TATAMOTORS)
6. Global Commodities (GOLD/XAU-USD, SILVER/XAG-USD, WTI/OIL, BRENT, NATGAS, COPPER)
"""

import re
from typing import Optional

# Canonical symbol metadata: (display_name, exchange, asset_class, base_price, aliases)
GLOBAL_INSTRUMENTS: dict[str, dict] = {
    # --- CRYPTO (LAME-SPOT) ---
    "BTC-USDT": {
        "name": "Bitcoin / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 64250,
        "aliases": ["btc", "bitcoin", "btcusdt", "btc-usdt", "xbt"],
    },
    "ETH-USDT": {
        "name": "Ethereum / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 2640,
        "aliases": ["eth", "ethereum", "ethusdt", "eth-usdt", "ether", "layer-2", "rollup"],
    },
    "SOL-USDT": {
        "name": "Solana / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 178,
        "aliases": ["sol", "solana", "solusdt", "sol-usdt"],
    },
    "BNB-USDT": {
        "name": "BNB / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 595,
        "aliases": ["bnb", "binance coin", "bnbusdt", "bnb-usdt"],
    },
    "XRP-USDT": {
        "name": "XRP / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 1,
        "aliases": ["xrp", "ripple", "xrpusdt", "xrp-usdt"],
    },
    "DOGE-USDT": {
        "name": "Dogecoin / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 1,
        "aliases": ["doge", "dogecoin", "dogeusdt", "doge-usdt"],
    },
    "ADA-USDT": {
        "name": "Cardano / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 1,
        "aliases": ["ada", "cardano", "adausdt", "ada-usdt"],
    },
    "AVAX-USDT": {
        "name": "Avalanche / Tether",
        "exchange": "LAME-SPOT",
        "asset_class": "crypto",
        "price": 28,
        "aliases": ["avax", "avalanche", "avaxusdt", "avax-usdt"],
    },
    # --- US EQUITIES: NASDAQ & NYSE TOP 10 ---
    "NVDA": {
        "name": "NVIDIA Corporation",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 138,
        "aliases": ["nvda", "nvidia", "blackwell", "geforce", "cuda", "nvda-usd"],
    },
    "AAPL": {
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 228,
        "aliases": ["aapl", "apple", "iphone", "macbook", "aapl-usd"],
    },
    "MSFT": {
        "name": "Microsoft Corporation",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 425,
        "aliases": ["msft", "microsoft", "azure", "copilot", "msft-usd"],
    },
    "AMZN": {
        "name": "Amazon.com Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 210,
        "aliases": ["amzn", "amazon", "aws", "trainium", "amzn-usd"],
    },
    "META": {
        "name": "Meta Platforms Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 592,
        "aliases": ["meta", "facebook", "instagram", "whatsapp", "llama", "meta-usd"],
    },
    "TSLA": {
        "name": "Tesla Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 258,
        "aliases": ["tsla", "tesla", "cybercab", "megapack", "fsd", "tsla-usd"],
    },
    "GOOGL": {
        "name": "Alphabet Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 182,
        "aliases": ["googl", "goog", "alphabet", "google", "waymo", "youtube", "googl-usd"],
    },
    "AVGO": {
        "name": "Broadcom Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 176,
        "aliases": ["avgo", "broadcom", "vmware", "avgo-usd"],
    },
    "BRK.B": {
        "name": "Berkshire Hathaway Inc.",
        "exchange": "NYSE",
        "asset_class": "stock",
        "price": 456,
        "aliases": ["brk.b", "brk-b", "brkb", "berkshire", "buffett"],
    },
    "JPM": {
        "name": "JPMorgan Chase & Co.",
        "exchange": "NYSE",
        "asset_class": "stock",
        "price": 234,
        "aliases": ["jpm", "jpmorgan", "jp morgan", "chase", "jpm-usd"],
    },
    "AMD": {
        "name": "Advanced Micro Devices Inc.",
        "exchange": "NASDAQ",
        "asset_class": "stock",
        "price": 152,
        "aliases": ["amd", "advanced micro devices", "instinct", "epyc", "ryzen"],
    },
    "LLY": {
        "name": "Eli Lilly and Company",
        "exchange": "NYSE",
        "asset_class": "stock",
        "price": 890,
        "aliases": ["lly", "eli lilly", "lilly", "zepbound", "mounjaro"],
    },
    # --- EUROPEAN EQUITIES: LSE, EURONEXT, XETRA TOP 10 ---
    "ASML": {
        "name": "ASML Holding N.V.",
        "exchange": "EURONEXT",
        "asset_class": "stock",
        "price": 685,
        "aliases": ["asml", "euv", "lithography", "asml-eur"],
    },
    "LVMH": {
        "name": "LVMH Moët Hennessy Louis Vuitton",
        "exchange": "EURONEXT",
        "asset_class": "stock",
        "price": 630,
        "aliases": ["lvmh", "mc.pa", "louis vuitton", "moet", "hennessy", "dior"],
    },
    "NVO": {
        "name": "Novo Nordisk A/S",
        "exchange": "OMX-CPH",
        "asset_class": "stock",
        "price": 112,
        "aliases": ["nvo", "novo", "novo nordisk", "novo-b", "wegovy", "ozempic"],
    },
    "SAP": {
        "name": "SAP SE",
        "exchange": "XETRA",
        "asset_class": "stock",
        "price": 222,
        "aliases": ["sap", "sap.de", "s4hana"],
    },
    "SHEL": {
        "name": "Shell plc",
        "exchange": "LSE",
        "asset_class": "stock",
        "price": 68,
        "aliases": ["shel", "shell", "royal dutch shell"],
    },
    "AZN": {
        "name": "AstraZeneca plc",
        "exchange": "LSE",
        "asset_class": "stock",
        "price": 148,
        "aliases": ["azn", "astrazeneca", "astra zeneca"],
    },
    "HSBA": {
        "name": "HSBC Holdings plc",
        "exchange": "LSE",
        "asset_class": "stock",
        "price": 46,
        "aliases": ["hsba", "hsbc"],
    },
    "SIE": {
        "name": "Siemens AG",
        "exchange": "XETRA",
        "asset_class": "stock",
        "price": 184,
        "aliases": ["sie", "sie.de", "siemens"],
    },
    "TTE": {
        "name": "TotalEnergies SE",
        "exchange": "EURONEXT",
        "asset_class": "stock",
        "price": 64,
        "aliases": ["tte", "totalenergies", "total energies"],
    },
    "ULVR": {
        "name": "Unilever plc",
        "exchange": "LSE",
        "asset_class": "stock",
        "price": 58,
        "aliases": ["ulvr", "unilever", "una"],
    },
    # --- ASIA-PACIFIC EQUITIES: TSE, HKEX, TWSE, KRX TOP 10 ---
    "TSM": {
        "name": "Taiwan Semiconductor Manufacturing Co. (TSMC)",
        "exchange": "TWSE/NYSE",
        "asset_class": "stock",
        "price": 194,
        "aliases": ["tsm", "tsmc", "2330.tw", "2330", "taiwan semiconductor", "cowos"],
    },
    "0700.HK": {
        "name": "Tencent Holdings Ltd.",
        "exchange": "HKEX",
        "asset_class": "stock",
        "price": 418,
        "aliases": ["0700.hk", "0700", "tcehy", "tencent", "wechat"],
    },
    "BABA": {
        "name": "Alibaba Group Holding Ltd.",
        "exchange": "HKEX/NYSE",
        "asset_class": "stock",
        "price": 98,
        "aliases": ["baba", "9988.hk", "9988", "alibaba", "alicloud", "taobao", "tmall"],
    },
    "TM": {
        "name": "Toyota Motor Corporation",
        "exchange": "TSE/NYSE",
        "asset_class": "stock",
        "price": 176,
        "aliases": ["tm", "7203.t", "7203", "toyota"],
    },
    "005930.KS": {
        "name": "Samsung Electronics Co., Ltd.",
        "exchange": "KRX",
        "asset_class": "stock",
        "price": 58000,
        "aliases": ["005930.ks", "005930", "samsung", "ssnlf", "hbm3e"],
    },
    "SONY": {
        "name": "Sony Group Corporation",
        "exchange": "TSE/NYSE",
        "asset_class": "stock",
        "price": 96,
        "aliases": ["sony", "6758.t", "6758", "playstation"],
    },
    "BYD": {
        "name": "BYD Company Limited",
        "exchange": "HKEX/SZSE",
        "asset_class": "stock",
        "price": 292,
        "aliases": ["byd", "1211.hk", "1211", "byddy"],
    },
    "MUFG": {
        "name": "Mitsubishi UFJ Financial Group",
        "exchange": "TSE/NYSE",
        "asset_class": "stock",
        "price": 11,
        "aliases": ["mufg", "8306.t", "8306", "mitsubishi ufj"],
    },
    "3690.HK": {
        "name": "Meituan",
        "exchange": "HKEX",
        "asset_class": "stock",
        "price": 185,
        "aliases": ["3690.hk", "3690", "meituan"],
    },
    "9984.T": {
        "name": "SoftBank Group Corp.",
        "exchange": "TSE",
        "asset_class": "stock",
        "price": 9100,
        "aliases": ["9984.t", "9984", "sftby", "softbank"],
    },
    # --- INDIAN EQUITIES: NSE & BSE TOP 10 ---
    "RELIANCE": {
        "name": "Reliance Industries Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 1295,
        "aliases": ["reliance", "reliance.ns", "ril", "jio", "reliance-inr"],
    },
    "TCS": {
        "name": "Tata Consultancy Services Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 4120,
        "aliases": ["tcs", "tcs.ns", "tata consultancy"],
    },
    "HDFCBANK": {
        "name": "HDFC Bank Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 1745,
        "aliases": ["hdfcbank", "hdfcbank.ns", "hdfc", "hdfc bank"],
    },
    "INFY": {
        "name": "Infosys Limited",
        "exchange": "NSE/NYSE",
        "asset_class": "stock",
        "price": 1860,
        "aliases": ["infy", "infy.ns", "infosys"],
    },
    "ICICIBANK": {
        "name": "ICICI Bank Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 1280,
        "aliases": ["icicibank", "icicibank.ns", "icici", "icici bank", "ibn"],
    },
    "BHARTIARTL": {
        "name": "Bharti Airtel Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 1610,
        "aliases": ["bhartiartl", "airtel", "bharti airtel"],
    },
    "SBIN": {
        "name": "State Bank of India",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 845,
        "aliases": ["sbin", "sbi", "state bank of india"],
    },
    "LT": {
        "name": "Larsen & Toubro Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 3620,
        "aliases": ["lt", "larsen", "larsen & toubro", "l&t"],
    },
    "ITC": {
        "name": "ITC Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 482,
        "aliases": ["itc", "itc.ns"],
    },
    "TATAMOTORS": {
        "name": "Tata Motors Limited",
        "exchange": "NSE/BSE",
        "asset_class": "stock",
        "price": 840,
        "aliases": ["tatamotors", "tata motors", "jlr"],
    },
    # --- COMMODITIES (COMEX / NYMEX / ICE) ---
    "GOLD": {
        "name": "Spot Gold (XAU/USD)",
        "exchange": "COMEX",
        "asset_class": "commodity",
        "price": 2735,
        "aliases": ["gold", "xau", "xau-usd", "xauusd", "bullion", "gold-usd"],
    },
    "SILVER": {
        "name": "Spot Silver (XAG/USD)",
        "exchange": "COMEX",
        "asset_class": "commodity",
        "price": 33,
        "aliases": ["silver", "xag", "xag-usd", "xagusd"],
    },
    "WTI": {
        "name": "WTI Crude Oil (CL)",
        "exchange": "NYMEX",
        "asset_class": "commodity",
        "price": 71,
        "aliases": ["wti", "oil", "crude", "crude oil", "cl-usd", "oil-usd"],
    },
    "BRENT": {
        "name": "Brent Crude Oil",
        "exchange": "ICE",
        "asset_class": "commodity",
        "price": 75,
        "aliases": ["brent", "brent crude"],
    },
    "NATGAS": {
        "name": "Henry Hub Natural Gas",
        "exchange": "NYMEX",
        "asset_class": "commodity",
        "price": 3,
        "aliases": ["natgas", "natural gas", "ng-usd", "lng"],
    },
    "COPPER": {
        "name": "High-Grade Copper",
        "exchange": "COMEX",
        "asset_class": "commodity",
        "price": 4,
        "aliases": ["copper", "hg-usd"],
    },
}


def resolve_symbol_from_text(text: str) -> list[str]:
    """Finds all canonical instrument symbols mentioned by ticker or company/asset alias in `text`."""
    if not text:
        return []
    q_lower = text.lower()
    matched: list[str] = []

    # Check exact ticker or alias word boundaries
    for sym, meta in GLOBAL_INSTRUMENTS.items():
        if sym.lower() in q_lower:
            matched.append(sym)
            continue
        for alias in meta["aliases"]:
            pattern = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
            if re.search(pattern, q_lower):
                matched.append(sym)
                break
    return matched


def normalize_instrument_symbol(raw_symbol: Optional[str]) -> Optional[str]:
    """Normalizes any user/service symbol (e.g. 'aapl', 'AAPL-USD', 'btc', 'gold') to its canonical key."""
    if not raw_symbol or not raw_symbol.strip():
        return None
    s = raw_symbol.strip().upper()
    if s in GLOBAL_INSTRUMENTS:
        return s
    # Check stripped quote suffixes like AAPL-USD, NVDA-USDT, GOLD-USD
    for suffix in ("-USDT", "-USD", "-EUR", "-GBP", "-INR", "-JPY", "-HKD"):
        if s.endswith(suffix):
            base = s[: -len(suffix)]
            if base in GLOBAL_INSTRUMENTS:
                return base
            if f"{base}-USDT" in GLOBAL_INSTRUMENTS:
                return f"{base}-USDT"
    matches = resolve_symbol_from_text(s)
    if matches:
        return matches[0]
    return s
