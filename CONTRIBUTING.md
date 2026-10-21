# 参与贡献

欢迎提交 issue 和 pull request。

## 比较需要帮助的方向

- 接入官方海外数据源：台湾 TWSE OpenAPI / MOPS，韩国 DART，日本 EDINET（日本季度数据）。
- 新行业的配置：在 `config/sectors.json` 里增加行业、环节、龙头和需求证据。
- 更好的分类规则，减少 SIC 误归类。
- 排序因子的回测。

## 约定

- 只用 Python 标准库，保持零依赖。
- 命令行输出用英文；页面文字和配置说明可以用中文。
- 需求证据要附来源链接和来源等级（primary / authoritative / general / unverified），不要写没有出处的数字。
- 不要提交任何抓取下来的数据（`data/`、`site/` 已在 `.gitignore` 中）。
- 不要提交密钥、token 或个人邮箱。SEC 联系方式通过环境变量 `SEC_USER_AGENT` 提供。

## 本地验证

```bash
python -m radar build     # 用已有快照生成页面，检查有没有报错
```

改动了模板之后，请在浏览器里打开 `site/index.html`，把四个行业都切换一遍。
