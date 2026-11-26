# QDII Observatory

标普500 / 纳斯达克100 场内 QDII 与防守资产公开行情看板。两组 QDII 各有收益图、溢价图及同指数评分表，并提供 VOO / QQQ 美元和人民币收益参考。防守资产组提供分红再投资收益图。只处理公开市场数据。

Python 3.12、Node.js 22，无需安装第三方依赖。网页为原生 HTML/CSS/JavaScript，部署后无需运行后端服务。

## 部署到 GitHub

1. 将本目录作为独立 Git 仓库推送到 GitHub，默认分支使用 `main`。`seed/state.tar.gz` 是公开历史启动数据，须一同提交。
2. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions → Update market and deploy Pages → Run workflow** 手动执行一次。此后每天北京时间 07:35 检查交易日历并按需更新发布。推送 `main` 时只跑测试，网站更新随下次发布上线。
4. 部署成功后，在工作流的 `github-pages` environment 中打开站点地址。支持 `https://用户名.github.io/仓库名/` 子路径。

工作流使用仓库自带的 `GITHUB_TOKEN`，无需额外配置密钥。组织策略须允许 Actions 写入 Releases、使用 Pages，以及所引用的官方 actions。项目不包含远程仓库地址；可先在 GitHub 创建空仓库，然后执行：

```bash
git add .
git commit -m "初始化独立 QDII 行情看板"
git remote add origin git@github.com:YOUR_NAME/qdii-observatory.git
git push -u origin main
```

## 本地使用

```bash
python3 scripts/site.py restore seed/state.tar.gz
python3 scripts/qdii_market.py update
python3 scripts/site.py build dist
python3 -m http.server 8876 --directory dist --bind 127.0.0.1
```

访问 `http://127.0.0.1:8876`。恢复启动包仅用于空目录初始化；已有新数据时不要重复恢复旧启动包。直接运行 `python3 scripts/qdii_market.py serve --port 8876` 可预览当前数据库；页面“重新读取”只读取数据，不启动采集。

## 文档

- [部署、调度、持久化与故障处理](docs/deployment.md)
- [数据来源、计算口径及交易日历](docs/qdii-market-data.md)

验证命令：

```bash
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
```
