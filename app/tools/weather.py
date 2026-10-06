"""天气查询相关工具"""
from pydantic_ai import RunContext


def get_current_weather(ctx: RunContext[None], city: str) -> str:
    """获取指定城市的天气信息。

    Args:
        city: 城市名称，如 '北京', '深圳', 'Tokyo'
    """
    weather_data = {
        "北京": "晴朗，气温 18°C，微风",
        "深圳": "多云转阵雨，气温 26°C，湿度 78%",
        "上海": "阴，气温 20°C，东南风 2 级",
        "tokyo": "晴，气温 22°C",
    }
    return weather_data.get(city.lower(), weather_data.get(city, f"{city} 天气晴朗，适宜出行。"))
