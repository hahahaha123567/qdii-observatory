# QDII Observatory

本项目只处理公开市场数据，不引入个人持仓、账单、账号或资产报告。

- Python 3.12 标准库、SQLite、原生 JavaScript，无前端构建依赖
- 数据口径与交易日规则见 docs/qdii-market-data.md；部署和状态持久化见 docs/deployment.md
- 原始响应只新增归档，不直接修改；数据库与原件不进入源码提交，seed/state.tar.gz 是审核过的公开历史启动包
- 页面固定为两个指数的四图与评分表，保持分红、拆分、统一日期等既有口径
- 修改后运行 python3 -m unittest discover -s tests 和 node --test tests/*.test.mjs
- 新年度前维护 config/qdii_trading_calendar.json；不能把未知年份当普通工作日
