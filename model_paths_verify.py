"""独立验证脚本：model_paths.py 的本地缓存解析（离线，秒跑，不 import funasr）。

  python model_paths_verify.py           # 全量断言
  python model_paths_verify.py --demo    # 打印本机真实解析结果
"""
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import model_paths

N = [0]
FAILED = []


def check(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  [OK]   %s" % label)
    else:
        FAILED.append(label)
        print("  [FAIL] %s  %s" % (label, detail))


def make_cache(root, layout="hub", asr_marker=True, vad_marker=True):
    """造一份假的 modelscope 缓存。layout 决定目录层级。"""
    bases = {
        "hub": os.path.join(root, "hub", "models", "iic"),
        "models": os.path.join(root, "models", "iic"),
        "iic": os.path.join(root, "iic"),
    }
    base = bases[layout]
    asr = os.path.join(base, "SenseVoiceSmall")
    vad = os.path.join(base, "speech_fsmn_vad_zh-cn-16k-common-pytorch")
    os.makedirs(asr, exist_ok=True)
    os.makedirs(vad, exist_ok=True)
    if asr_marker:
        open(os.path.join(asr, "model.pt"), "wb").close()
    if vad_marker:
        open(os.path.join(vad, "config.yaml"), "w").close()
    return asr, vad


def run():
    print("=" * 62)
    print("model_paths 离线验证")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="modelpaths_")
    try:
        # 环境干净，避免用户机器上的真实环境变量影响断言
        clean = {k: v for k, v in os.environ.items()
                 if k not in (model_paths.CACHE_ENV,
                              model_paths.MODEL_DIR_ENV,
                              model_paths.FORCE_ONLINE_ENV)}
        # 把「默认 home 缓存」也指向临时区，否则断言「缓存不存在应退回 ID」
        # 时会被用户机器上真实的 936MB 模型缓存命中，测不到退回分支
        with patch.dict(os.environ, clean, clear=True), \
             patch.object(model_paths, "default_cache_iic",
                          return_value=os.path.join(tmp, "no_home_iic")):
            root = os.path.join(tmp, "cache1")
            asr, vad = make_cache(root)

            got = model_paths.resolve_model_path(
                "iic/SenseVoiceSmall", cache_root=root)
            check("缓存完整 -> 解析为本地路径", got != "iic/SenseVoiceSmall",
                  repr(got))
            check("解析结果是绝对路径", os.path.isabs(got), repr(got))
            check("指向正确的模型目录", got == os.path.abspath(asr), repr(got))
            check("marker 文件确实存在",
                  os.path.isfile(os.path.join(got, "model.pt")))

            got_vad = model_paths.resolve_model_path(
                "fsmn-vad", cache_root=root)
            check("fsmn-vad -> 正确的本地目录",
                  got_vad == os.path.abspath(vad), repr(got_vad))

            check("两个模型路径不冲突", got != got_vad)

            # 缓存缺关键文件 -> 必须退回模型 ID，让 funasr 自己下载
            root2 = os.path.join(tmp, "cache2")
            make_cache(root2, asr_marker=False, vad_marker=False)
            check("缺 model.pt -> 退回模型 ID",
                  model_paths.resolve_model_path(
                      "iic/SenseVoiceSmall", cache_root=root2)
                  == "iic/SenseVoiceSmall")
            check("缺 config.yaml -> 退回模型 ID",
                  model_paths.resolve_model_path(
                      "fsmn-vad", cache_root=root2) == "fsmn-vad")

            check("未知模型 ID 原样返回",
                  model_paths.resolve_model_path("some/other-model",
                                                cache_root=root)
                  == "some/other-model")

            # 强制在线：用于以后要更新模型
            with patch.dict(os.environ, {model_paths.FORCE_ONLINE_ENV: "1"}):
                check("BILIMUSIC_MODEL_ONLINE=1 -> 强制走在线校验",
                      model_paths.resolve_model_path(
                          "iic/SenseVoiceSmall", cache_root=root)
                      == "iic/SenseVoiceSmall")
                check("强制在线对 vad 同样生效",
                      model_paths.resolve_model_path("fsmn-vad",
                                                     cache_root=root)
                      == "fsmn-vad")

            # 环境变量的三种常见层级都要能命中
            for layout in ("hub", "models", "iic"):
                r = os.path.join(tmp, "env_" + layout)
                expected, _ = make_cache(r, layout=layout)
                with patch.dict(os.environ, {model_paths.CACHE_ENV: r}):
                    got = model_paths.resolve_model_path("iic/SenseVoiceSmall")
                check("MODELSCOPE_CACHE 层级 %-6s 能命中" % layout,
                      got == os.path.abspath(expected), repr(got))

            # ---- BILIMUSIC_MODEL_DIR：项目级自定义模型位置 ----
            # 将来加 SSD / 换电脑时，把两个模型目录拷过去 + 设这个变量即可
            for layout in ("hub", "models", "iic"):
                r = os.path.join(tmp, "custom_" + layout)
                expected, _ = make_cache(r, layout=layout)
                with patch.dict(os.environ, {model_paths.MODEL_DIR_ENV: r}):
                    got = model_paths.resolve_model_path("iic/SenseVoiceSmall")
                check("BILIMUSIC_MODEL_DIR 层级 %-6s 能命中" % layout,
                      got == os.path.abspath(expected), repr(got))

            simple = os.path.join(tmp, "simple_models")
            os.makedirs(os.path.join(simple, "SenseVoiceSmall"))
            open(os.path.join(simple, "SenseVoiceSmall", "model.pt"),
                 "wb").close()
            simple_asr = os.path.abspath(
                os.path.join(simple, "SenseVoiceSmall"))
            with patch.dict(os.environ, {model_paths.MODEL_DIR_ENV: simple}):
                check("BILIMUSIC_MODEL_DIR 最简布局 <dir>/<leaf> 能命中",
                      model_paths.resolve_model_path("iic/SenseVoiceSmall")
                      == simple_asr)
                check("自定义目录里缺 vad -> 退回模型 ID",
                      model_paths.resolve_model_path("fsmn-vad") == "fsmn-vad")

            with patch.dict(os.environ, {
                    model_paths.MODEL_DIR_ENV: simple,
                    model_paths.CACHE_ENV: root}):
                check("自定义目录优先于 MODELSCOPE_CACHE",
                      model_paths.resolve_model_path("iic/SenseVoiceSmall")
                      == simple_asr)

            empty = os.path.join(tmp, "empty_custom")
            with patch.dict(os.environ, {
                    model_paths.MODEL_DIR_ENV: empty,
                    model_paths.CACHE_ENV: root}):
                check("自定义目录找不到时回落到 MODELSCOPE_CACHE",
                      model_paths.resolve_model_path("iic/SenseVoiceSmall")
                      == os.path.abspath(asr))
            with patch.dict(os.environ, {model_paths.MODEL_DIR_ENV: empty}):
                check("自定义目录为空且无缓存 -> 退回模型 ID",
                      model_paths.resolve_model_path("iic/SenseVoiceSmall")
                      == "iic/SenseVoiceSmall")

            with patch.dict(os.environ, {
                    model_paths.MODEL_DIR_ENV: simple,
                    model_paths.FORCE_ONLINE_ENV: "1"}):
                check("强制在线时自定义目录也跳过",
                      model_paths.resolve_model_path("iic/SenseVoiceSmall")
                      == "iic/SenseVoiceSmall")
            check("describe() 能显示自定义目录",
                  simple_asr in model_paths.describe(
                      "iic/SenseVoiceSmall", model_dir=simple))

            # 空目录：不能误判成缓存
            check("缓存目录不存在 -> 退回模型 ID",
                  model_paths.resolve_model_path(
                      "iic/SenseVoiceSmall",
                      cache_root=os.path.join(tmp, "nope"))
                  == "iic/SenseVoiceSmall")

            check("describe() 带 [本地] 标记",
                  "[本地]" in model_paths.describe(
                      "iic/SenseVoiceSmall", cache_root=root))
            check("describe() 在线模式有提示",
                  "在线模式" in model_paths.describe(
                      "iic/SenseVoiceSmall",
                      cache_root=os.path.join(tmp, "nope")))

            # 纯函数：不碰磁盘、不联网、不 import 重库
            check("模块只依赖 os（无 funasr/torch/requests）",
                  "import funasr" not in open(
                      model_paths.__file__, encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 62)
    print("结果：%d/%d 通过" % (N[0] - len(FAILED), N[0]))
    if FAILED:
        for f in FAILED:
            print("  失败：%s" % f)
        return 1
    return 0


def demo():
    print("=" * 62)
    print("本机真实解析结果")
    print("=" * 62)
    for mid in ("iic/SenseVoiceSmall", "fsmn-vad"):
        print("  " + model_paths.describe(mid))
    print()
    print("  BILIMUSIC_MODEL_DIR = %s" % (
        os.environ.get(model_paths.MODEL_DIR_ENV) or "(未设，用默认缓存)"))
    print("  MODELSCOPE_CACHE    = %s" % (
        os.environ.get(model_paths.CACHE_ENV) or "(未设)"))
    print()
    print("  换盘：把两个模型目录拷到新位置，设 BILIMUSIC_MODEL_DIR=<新位置>")
    print("  设 BILIMUSIC_MODEL_ONLINE=1 可临时恢复在线校验（更新模型时用）")
    return 0


if __name__ == "__main__":
    sys.exit(demo() if "--demo" in sys.argv else run())
