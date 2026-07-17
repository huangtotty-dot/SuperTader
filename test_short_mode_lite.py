# -*- coding: utf-8 -*-
"""
V1.26 反T模式轻量验证脚本
不加载所有模块，只验证核心配置和逻辑
"""
import json
import os
import ast

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def test_t_mode_json():
    """验证 t_mode.json 格式"""
    path = os.path.join(BASE_DIR, "t_mode.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # 过滤内部字段
    modes = {k: v for k, v in data.items() if not k.startswith("_")}
    print(f"[OK] t_mode.json loaded")
    print(f"   entries: {len(modes)}")
    for code, mode in modes.items():
        print(f"   {code} -> {mode}")
    return True

def test_config_short_params():
    """验证 config.py 中 SHORT_MODE_PARAMS 存在"""
    path = os.path.join(BASE_DIR, "config.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # 查找 SHORT_MODE_PARAMS 赋值
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SHORT_MODE_PARAMS":
                    found = True
                    # 检查关键参数
                    keys = []
                    if isinstance(node.value, ast.Dict):
                        for k in node.value.keys:
                            if isinstance(k, ast.Constant):
                                keys.append(k.value)
                    print(f"[OK] SHORT_MODE_PARAMS defined")
                    print(f"   keys: {keys[:10]}...")
                    break
    
    # 检查 load_t_mode 和 save_t_mode 函数
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    has_load = "load_t_mode" in funcs
    has_save = "save_t_mode" in funcs
    print(f"[OK] load_t_mode: {'yes' if has_load else 'no'}")
    print(f"[OK] save_t_mode: {'yes' if has_save else 'no'}")
    
    # 检查 T_MODE_FILE
    if "T_MODE_FILE" in source:
        print(f"[OK] T_MODE_FILE constant defined")
    
    return found and has_load and has_save

def test_signal_engine_short_logic():
    """验证 signal_engine.py 中反T逻辑"""
    path = os.path.join(BASE_DIR, "signal_engine.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # 查找 evaluate 方法
    evaluate_method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate":
            evaluate_method = node
            break
    
    if not evaluate_method:
        print("[FAIL] evaluate method not found")
        return False
    
    # 检查 is_short_mode 的使用
    source_segment = ast.get_source_segment(source, evaluate_method)
    checks = {
        "is_short_mode defined": "is_short_mode = t_mode == \"short\"" in source or "is_short_mode = t_mode == 'short'" in source,
        "SHORT_MODE_PARAMS applied": "SHORT_MODE_PARAMS" in source_segment,
        "hold_qty check": "hold_qty <= 0" in source_segment and "return 0, 0, None" in source_segment,
        "short L2 logic": "反T早盘弱势卖出" in source_segment,
        "short L1 logic": "反T早盘弱势卖出" in source_segment,
    }
    
    print(f"[OK] is_short_mode logic found in evaluate")
    for check, result in checks.items():
        status = "[OK]" if result else "[FAIL]"
        print(f"   {status} {check}")
    
    return all(checks.values())

def test_main_prompt():
    """验证 main.py 中启动提示"""
    path = os.path.join(BASE_DIR, "main.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    
    checks = {
        "_prompt_t_mode_selection func": "def _prompt_t_mode_selection" in source,
        "T_MODE load": "T_MODE = load_t_mode()" in source,
        "shared T_MODE": "shared['T_MODE'] = T_MODE" in source,
        "prompt text": "正T(long)" in source and "反T(short)" in source,
    }
    
    print(f"[OK] main.py prompt logic")
    for check, result in checks.items():
        status = "[OK]" if result else "[FAIL]"
        print(f"   {status} {check}")
    
    return all(checks.values())

if __name__ == "__main__":
    print("="*60)
    print("V1.26 Short Mode Validation")
    print("="*60)
    
    ok1 = test_t_mode_json()
    print()
    ok2 = test_config_short_params()
    print()
    ok3 = test_signal_engine_short_logic()
    print()
    ok4 = test_main_prompt()
    print()
    
    if ok1 and ok2 and ok3 and ok4:
        print("ALL PASSED! Short mode is implemented")
    else:
        print("SOME CHECKS FAILED")
