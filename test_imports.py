import sys
sys.path.insert(0, '.')

# Test config module
try:
    from app.etoro.config import EtoroConfig
    print("Config import successful")
    c = EtoroConfig()
    print("Config creation successful")
    print("secret_key:", c.secret_key)
    print("llm_url:", c.llm_url)
    print("max_open_positions:", c.max_open_positions)
    print("max_position_size:", c.max_position_size)
except Exception as e:
    print("Config import failed:", e)

# Test guards module
try:
    from app.safety.guards import check_kill_switch, check_daily_loss, check_drawdown, check_position_size
    print("Guards import successful")
except Exception as e:
    print("Guards import failed:", e)