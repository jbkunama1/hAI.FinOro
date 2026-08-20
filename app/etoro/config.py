# app/etoro/config.py
from pydantic import Field, PositiveFloat, PositiveInt, conint
from pydantic_settings import BaseSettings, SettingsConfigDict, field_validator
from typing import Optional

class EtoroConfig(BaseSettings):
    # Authentication
    api_key: str = Field(..., env="ETORO_API_KEY")
    user_key: str = Field(..., env="ETORO_USER_KEY")
    
    # Secrets
    secret_key: str = Field(default="change-me")
    
    # Core URLs
    api_url: str = Field(..., env="ETORO_API_URL")
    base_url: str = Field(..., env="ETORO_BASE_URL")
    
    # Features
    sandbox: bool = Field(..., env="ETORO_SANDBOX", description="Use sandbox (demo) mode")
    execution_enabled: bool = Field(False, env="ETORO_EXECUTION_ENABLED", description="Enable live trading")
    kill_switch: bool = Field(True, env="ETORO_KILL_SWITCH", description="Global kill switch")
    confirmation_phrase: str = Field("CONFIRM", env="ETORO_CONFIRMATION_PHRASE")
    
    # Limits
    max_trade_usd: PositiveFloat = Field(500.0, env="ETORO_MAX_TRADE_USD")
    max_daily_trade_usd: PositiveFloat = Field(1000.0, env="ETORO_MAX_DAILY_TRADE_USD")
    # Position limits
    max_open_positions: PositiveInt = Field(25, env="ETORO_MAX_OPEN_POSITIONS")
    max_position_size: PositiveFloat = Field(25.0, env="ETORO_MAX_TRADE_SIZE")
    symbol_allowlist: list[str] = Field(default_factory=list, env="ETORO_SYMBOL_ALLOWLIST")
    
    # Trading schedule
    trade_start: str = Field("08:00", env="ETORO_TRADE_START")
    trade_end: str = Field("22:00", env="ETORO_TRADE_END")
    
    # Risk limits (example defaults)
    max_daily_loss: PositiveFloat = Field(100.0, env="ETORO_MAX_DAILY_LOSS")
    max_drawdown_pct: PositiveFloat = Field(10.0, env="ETORO_MAX_DRAWDOWN_PCT")
    risk_per_trade_pct: PositiveFloat = Field(5.0, env="ETORO_RISK_PER_TRADE_PCT")
    
    # LLM
    llm_url: str = Field(default="https://9router.arbeitermili.eu/v1", env="LLM_URL")
    llm_model: str = Field(default="finance", env="LLM_MODEL")
    llm_api_key: str = Field(default_factory=lambda: "", env="LLM_API_KEY")

    # Misc
    app_port: int = Field(8080, env="APP_PORT")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("symbol_allowlist", mode="before")
    def allowlist_list(cls, v):
        if isinstance(v, str):
            # allow direct comma-separated string
            return [s.strip() for s in v.split(",") if s.strip()]
        return v
