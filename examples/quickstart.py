import sys
sys.stdout.reconfigure(encoding='utf-8')
import bfinance as bf

print("=" * 60)
print("TESTING BFINANCE LIVE WITH MULTIPLE INDIAN EQUITIES")
print("=" * 60)

for sym in ["TCS", "BAJAJ-AUTO", "3MINDIA"]:
    t = bf.Ticker(sym)
    info = t.info
    print(f"\n--- {sym} ---")
    print(f"Name: {info.get('shortName')} | CMP: ₹{t.fast_info.last_price}")
    print(f"Market Cap: ₹{info.get('marketCapInCr'):,.1f} Cr")
    print(f"ROCE: {info.get('returnOnCapitalEmployed')}% | ROE: {info.get('returnOnEquity')}")
    print(f"Pros: {t.pros_cons.get('pros')[:2]}")
    print(f"Concalls ({len(t.concalls)} found):")
    for c in t.concalls[:2]:
        print(f"  • {c.date}: {c.title}")
        if c.transcript_url:
            print(f"    Transcript: {c.transcript_url}")

print("\n" + "=" * 60)
print("TESTING OPTIONS CHAIN")
print("=" * 60)
tcs = bf.Ticker("TCS")
chain = tcs.option_chain()
print(chain.calls[["contractSymbol", "strike", "lastPrice", "openInterest", "impliedVolatility"]].head(3))
