"""把 funasr 的模型 ID 解析成 modelscope 本地缓存的绝对路径。

为什么需要这个（2026-11-15 实测定位）：
  funasr/download/download_model_from_hub.py::download_from_ms() 里是

      if not os.path.exists(model_or_path) and "model_path" not in kwargs:
          ... get_or_download_model_dir() -> snapshot_download(...)

  传模型 ID（"iic/SenseVoiceSmall"）时 os.path.exists() **恒为 False**，
  所以每次 AutoModel() 都会去 modelscope 校验一遍 revision / 文件清单，
  哪怕 936MB 的 model.pt 早就在硬盘上。改传本地缓存的绝对路径后这个
  分支直接跳过（实测：零网络连接）。

  缓存不完整或不存在时原样返回模型 ID —— 首次运行仍能自动下载。
  设 BILIMUSIC_MODEL_ONLINE=1 可强制走原来的在线校验（用于更新模型）。

换模型存放位置（2026-11-15 加）：
  设 BILIMUSIC_MODEL_DIR=<目录>，本程序优先在那里找模型，支持三种布局：
      <dir>/<leaf>/                    （直接把两个模型目录拷进去）
      <dir>/hub/models/iic/<leaf>/    （modelscope 缓存根的层级）
      <dir>/models/iic/<leaf>/
  它只管「已有模型的查找」：解析成功后交给 AutoModel 的是绝对路径，
  funasr 不会再碰 modelscope 缓存。要让**首次下载**也落到别的盘，另外设
  MODELSCOPE_CACHE=<dir>（modelscope 会自己在下面建 hub/models/iic/...）。
  两个变量都不设时，行为与原来完全一致（默认 ~/.cache/modelscope）。
"""
import os

CACHE_ENV = "MODELSCOPE_CACHE"
MODEL_DIR_ENV = "BILIMUSIC_MODEL_DIR"
FORCE_ONLINE_ENV = "BILIMUSIC_MODEL_ONLINE"

# 模型 ID -> (modelscope 缓存里的叶子目录名, 必须存在的关键文件)
MODEL_LAYOUT = {
    "iic/SenseVoiceSmall": ("SenseVoiceSmall", "model.pt"),
    "fsmn-vad": (
        "speech_fsmn_vad_zh-cn-16k-common-pytorch", "config.yaml"),
}


def default_cache_iic():
    """默认的 modelscope 模型缓存目录（iic 组织下）。抽出来便于离线测试替换。"""
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "modelscope", "hub", "models", "iic")


def candidate_roots(leaf, cache_root=None, model_dir=None):
    """按优先级列出可能的模型目录。

    优先级：BILIMUSIC_MODEL_DIR > MODELSCOPE_CACHE > 默认 home 缓存。
    cache_root / model_dir 都可直接注入，方便离线测试；None 时读环境变量。
    """
    if cache_root is None:
        cache_root = os.environ.get(CACHE_ENV)
    if model_dir is None:
        model_dir = os.environ.get(MODEL_DIR_ENV)
    roots = []
    if model_dir:
        # 自定义目录优先：用户显式指定的位置永远赢过自动缓存
        roots.append(os.path.join(model_dir, leaf))
        roots.append(os.path.join(model_dir, "hub", "models", "iic", leaf))
        roots.append(os.path.join(model_dir, "models", "iic", leaf))
        roots.append(os.path.join(model_dir, "iic", leaf))
    if cache_root:
        # 环境变量既可能指 modelscope 根，也可能直接指 hub/models
        roots.append(os.path.join(cache_root, "hub", "models", "iic", leaf))
        roots.append(os.path.join(cache_root, "models", "iic", leaf))
        roots.append(os.path.join(cache_root, "iic", leaf))
    roots.append(os.path.join(default_cache_iic(), leaf))
    return roots


def resolve_model_path(model_id, cache_root=None, model_dir=None):
    """返回本地绝对路径；缓存不完整/不存在/强制在线时返回原 model_id。"""
    if os.environ.get(FORCE_ONLINE_ENV):
        return model_id
    layout = MODEL_LAYOUT.get(model_id)
    if layout is None:
        return model_id
    leaf, marker = layout
    for root in candidate_roots(leaf, cache_root, model_dir):
        if os.path.isfile(os.path.join(root, marker)):
            return os.path.abspath(root)
    return model_id


def describe(model_id, cache_root=None, model_dir=None):
    """给 CLI / 诊断用的一句话说明。"""
    resolved = resolve_model_path(model_id, cache_root, model_dir)
    if resolved == model_id:
        return "%s -> (在线模式，交给 funasr 下载)" % model_id
    return "%s -> %s [本地]" % (model_id, resolved)
