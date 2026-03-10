"""
Dashboard Chart Components - Real-time visualization for EvoClaw.
This module adds Chart.js-based visualization to the dashboard.
"""

# Chart configuration and data formatters for the dashboard
CHART_CONFIG = {
    "success_rate": {
        "type": "line",
        "label": "任務成功率（%）",
        "color": "#34d399",
        "max_points": 60
    },
    "queue_size": {
        "type": "bar",
        "label": "容器隊列大小",
        "color": "#60a5fa",
        "max_points": 60
    },
    "memory_usage": {
        "type": "gauge",
        "label": "記憶體使用量（MB）",
        "color": "#f59e0b",
        "max_points": 60
    }
}

def get_chart_data_template():
    """Return initial chart data structure."""
    return {
        "labels": [],
        "datasets": [
            {"label": "success_rate", "data": [], "borderColor": "#34d399"},
            {"label": "queue_size", "data": [], "borderColor": "#60a5fa"},
            {"label": "memory_mb", "data": [], "borderColor": "#f59e0b"}
        ]
    }
