import json
from pathlib import Path

def main():
    p = Path("state/remote_vm_state/daily_quota.json")
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        data["count"] = 50
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("Reset daily_quota.json count to 50")
    else:
        print("daily_quota.json does not exist")

if __name__ == "__main__":
    main()
