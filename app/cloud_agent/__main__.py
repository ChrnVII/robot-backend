"""允许 `python -m app.cloud_agent`：页面上的「重启进程」靠它 re-exec。"""

from .main import main

if __name__ == "__main__":
    main()
