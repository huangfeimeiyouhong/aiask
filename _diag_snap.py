import sys, time
sys.path.insert(0, ".")
from hcg_client import HCGClient
import config
from datetime import date
c = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
c.login("at0001", "at123456@")
ed = date.today().strftime("%Y-%m-%d")
for rd in [ed, "2026-07-30", "2026-07-01", "2026-06-30"]:
    t0 = time.time()
    try:
        d = c.page_stock_snapshot({"reportDate": rd, "pageNo": 1, "pageSize": 1})
        dt = time.time() - t0
        data = d.get("data") or {}
        print(rd, "success:", d.get("success"), "cost=%.1fs" % dt,
              "stockAmount:", data.get("stockAmount"),
              "total:", data.get("total"), "pages:", data.get("pages"),
              "msg:", d.get("message"))
    except Exception as e:
        print(rd, "EXC:", type(e).__name__, str(e)[:80], "cost=%.1fs" % (time.time() - t0))
