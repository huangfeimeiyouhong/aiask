import sys, time
sys.path.insert(0, ".")
from hcg_client import HCGClient
import semantic_tools as st
import config
from datetime import date
c = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
c.login("at0001", "at123456@")
ed = date.today().strftime("%Y-%m-%d")
t0 = time.time()
res, err = st.call_tool(c, "stock_snapshot", {"report_date": ed, "warehouse_name": "奥运餐厅"})
print("cost=%.1fs err=%s" % (time.time() - t0, err))
print("too_large:", res.get("too_large"), "| timeout:", res.get("timeout"))
if res.get("summary"):
    print("summary:", res.get("summary"))
    print("by_warehouse:", res.get("by_warehouse"))
    print("by_category count:", len(res.get("by_category", [])), "sample:", res.get("by_category", [])[:3])
    print("records_incomplete:", res.get("records_incomplete"))
