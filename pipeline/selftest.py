# selftest.py —— 离线 schema 自测（不碰浏览器、不加载模型、不提交）
import copy, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import validate_extract, validate_answer, check_value_against_options, gate

GOOD_EX = {"schema": "chaoxing-quiz-extract/1",
  "questions": [
    {"index": 1, "type": "单选题", "stem": "题干1", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}, {"key": "C", "text": "c"}, {"key": "D", "text": "d"}]},
    {"index": 2, "type": "多选题", "stem": "题干2", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}, {"key": "C", "text": "c"}, {"key": "D", "text": "d"}]},
    {"index": 3, "type": "判断题", "stem": "题干3", "options": [{"key": "A", "text": "对"}, {"key": "B", "text": "错"}]}]}

GOOD_AN = {"schema": "chaoxing-answer/1",
  "answers": [
    {"index": 1, "type": "单选题", "value": ["B"], "confidence": 0.95},
    {"index": 2, "type": "多选题", "value": ["A", "C"], "confidence": 0.9},
    {"index": 3, "type": "判断题", "value": ["A"], "confidence": 0.88}],
  "unknown": []}

FAILS = []

def check(name, cond, detail=""):
    ok = bool(cond)
    print(("PASS  " if ok else "FAIL  ") + name + (("  <- " + detail) if (not ok and detail) else ""))
    if not ok:
        FAILS.append(name)

# 1 正常
check("good extract valid", validate_extract(GOOD_EX) == [], str(validate_extract(GOOD_EX)))
check("good answers valid", validate_answer(GOOD_AN, [1, 2, 3]) == [], str(validate_answer(GOOD_AN, [1, 2, 3])))
check("good value in options", check_value_against_options(GOOD_AN, GOOD_EX) == [], str(check_value_against_options(GOOD_AN, GOOD_EX)))
check("good gate pass", gate(GOOD_AN, 0.8)["pass"] is True)

# 2 判断题三个选项 -> 失败
bad = copy.deepcopy(GOOD_EX); bad["questions"][2]["options"].append({"key": "C", "text": "x"})
check("judge with 3 options rejected", validate_extract(bad) != [])

# 3 缺题 -> 失败
bad = copy.deepcopy(GOOD_AN); bad["answers"] = bad["answers"][:2]
check("missing coverage rejected", validate_answer(bad, [1, 2, 3]) != [])

# 4 字母不在选项内 -> 失败
bad = copy.deepcopy(GOOD_AN); bad["answers"][0]["value"] = ["E"]
check("out-of-range letter rejected", check_value_against_options(bad, GOOD_EX) != [])

# 5 低置信度 -> 闸门失败
bad = copy.deepcopy(GOOD_AN); bad["answers"][1]["confidence"] = 0.5
check("low confidence blocks gate", gate(bad, 0.8)["pass"] is False)

# 6 unknown -> 闸门失败
bad = copy.deepcopy(GOOD_AN); bad["unknown"] = [{"index": 2, "reason": "no_data"}]; bad["answers"] = [bad["answers"][0], bad["answers"][2]]
check("unknown blocks gate", gate(bad, 0.8)["pass"] is False)

# 7 置信度越界 -> schema 失败
bad = copy.deepcopy(GOOD_AN); bad["answers"][0]["confidence"] = 1.7
check("confidence >1 rejected", validate_answer(bad, [1, 2, 3]) != [])

# 8 重复 index -> 失败
bad = copy.deepcopy(GOOD_AN); bad["answers"].append({"index": 1, "type": "单选题", "value": ["A"], "confidence": 0.9})
check("duplicate index rejected", validate_answer(bad, [1, 2, 3]) != [])

# 9 schema 版本错 -> 失败
bad = copy.deepcopy(GOOD_EX); bad["schema"] = "wrong/9"
check("bad schema version rejected", validate_extract(bad) != [])

print("")
if FAILS:
    print("SELFTEST FAILED: %d case(s): %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("SELFTEST ALL PASS (12 checks)")