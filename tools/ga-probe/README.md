# GitHub Actions 可行性探测（只验证"数据中心 IP 能不能拿到 Turnstile 令牌"）

想把签到挂到 GitHub Actions 上定时跑，卡点只有一个：**Cloudflare 会不会给 Actions 那台机器
（Azure 数据中心 IP）签发 Turnstile 令牌**。给了，整套方案才有意义；不给，后面的凭证、
状态回写全都白做。所以先花五分钟只测这一件事。

这个目录是**自包含**的，跟主项目没有依赖关系，也**不含任何凭证**（不登录、不签到、
不读 `data/`）。

## 怎么跑

1. 在 GitHub 上新建一个**空的私有仓库**（比如 `turnstile-probe`）。
   ⚠ 别把主项目推上去——`data/` 里有账号密码、TOTP 密钥、登录态、API Key
2. 把这个目录里的三个文件按下面的结构放进去：

   ```
   .github/workflows/probe.yml   ← 用本目录的 workflow-probe.yml 改名
   solve_probe.py
   requirements.txt
   ```
3. push 上去 → 仓库的 Actions 页 → 左边选 `turnstile-probe` → 点 **Run workflow**
   （可以填站点域名，默认 `site-b.example`）
4. 看日志最后一行

## 怎么判读

日志里有两步：先用 Cloudflare **官方测试 sitekey** 做对照，再用**站点真实 sitekey**。

| 对照组（测试 key） | 正题（真 key） | 结论 |
| --- | --- | --- |
| 拿到令牌 | 拿到令牌 | ✅ **可行**。数据中心 IP 不影响，可以做完整的 Actions 方案 |
| 拿到令牌 | 没拿到 | ❌ CF 认这个 IP/环境不可信。Actions 这条路对开着验证的站点走不通（没开验证的站点仍然可以走纯接口签到） |
| 没拿到 | 没拿到 | 环境本身有问题（浏览器没下好 / 缺系统库 / 网络被墙），先修这个再看 |

本机（住宅 IP、Windows）跑同一个脚本作为基准：

```powershell
python tools/ga-probe/solve_probe.py site-b.example
```

实测本机是**能拿到**的（约 800 字符的令牌，10 秒左右），所以如果 Actions 上拿不到，
差别就只剩出口 IP 和运行环境。

## 顺带说一下后面还有什么坑（等验证通过再管）

1. **凭证要出门**：`data/sessions/<站点>/*.json` 和 `data/tokens/<站点>.json` 等同于账号密码，
   放到 Actions 就得打包成 secret，意味着它们离开了你的机器
2. **状态要回写**：签到会轮换 session cookie、访问令牌偶尔被站点作废、`index.json` 里的
   `last_checkin` 也要更新。跑完得把新状态写回去（commit 回仓库 / 用 API 更新 secret），
   否则下次拿废票开局
3. **同一个 IP 打 60 个账号**：站点侧也可能风控（本地跑至少是住宅 IP）
4. **GitHub ToS**：Actions 明确写了不能用于"与本仓库软件的构建、测试、发布无关的活动"，
   定时刷第三方站点属于灰色地带，被扫到可能封仓库

不想折腾这些的话，本机 Windows 任务计划程序跑 `scripts\自动签到.cmd` 是最省事的：
现在签到全程无头、不占鼠标屏幕，正好适合挂后台。
