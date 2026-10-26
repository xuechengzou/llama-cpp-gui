# CI 配置

`github-actions.yml` 是 GitHub Actions 的工作流定义。

## 为什么放在这里而不是 `.github/workflows/`

推送工作流文件需要 token 具备 `workflow` 权限。当前用于初始化的
OAuth token 只有 `gist`、`read:org`、`repo` 三个 scope，因此直接推送
`.github/workflows/ci.yml` 会被 GitHub 拒绝：

```
refusing to allow an OAuth App to create or update workflow
`.github/workflows/ci.yml` without `workflow` scope
```

## 启用方法

任选其一。

### 方法一：补权限后自动部署

```bash
gh auth refresh -s workflow
mkdir -p .github/workflows
cp ci/github-actions.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: 启用 GitHub Actions"
git push
```

### 方法二：网页端粘贴

1. 打开仓库 → **Actions** 标签页 → 「set up a workflow yourself」
2. 把 `github-actions.yml` 的内容粘贴进去
3. 提交

### 方法三：用 Personal Access Token

在 GitHub 上生成一个带 `workflow` scope 的 PAT，然后用它推送：

```bash
git remote set-url origin https://<TOKEN>@github.com/xuechengzou/llama-cpp-gui.git
mkdir -p .github/workflows
cp ci/github-actions.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: 启用" && git push
```

## 本地手动检查

不想等 CI 可以本地跑等价检查：

```bash
python3 -m py_compile gui_server.py

python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('gui_server', 'gui_server.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
for fn in ('find_models','vram_info','model_params','read_gguf_meta','port_in_use'):
    assert hasattr(m, fn)
for t in ('f16','bf16','q8_0','q4_0'):
    assert t in m.KV_BYTES
for name, p in m.PRESETS.items():
    for k in ('ctx','parallel','fa','ctk','ctv'):
        assert k in p
print('✓ 全部检查通过')
"
```
