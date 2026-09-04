"""一次性迁移：把已发布的旧大脑 Prompt 强制升级为当前代码内嵌版本。

老库中已发布版本的 change_note 可能不是"系统初始版本"（例如经历过早期
系统升级），upgrade_marker 自动迁移不会触发。本脚本按内容比对：已发布
版本与代码内嵌不一致时，创建新草稿并发布（旧版本归档）。幂等。

    python scripts/server_migrate_brain_prompts.py [数据库路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

from quant_agent_harness.agent_prompts import (
    BRAIN_PLANNING_PROMPT,
    BRAIN_REVIEW_PROMPT,
    BRAIN_SYNTHESIS_PROMPT,
)
from quant_agent_harness.repository import Repository

PROMPT_ID_CONTENT = {
    "brain.planning": BRAIN_PLANNING_PROMPT,
    "brain.review": BRAIN_REVIEW_PROMPT,
    "coordinator.synthesis": BRAIN_SYNTHESIS_PROMPT,
}


def main() -> int:
    database = Path(sys.argv[1]) if len(sys.argv) > 1 else Repository().database_path
    repository = Repository(database)
    prompts = {item["prompt_id"]: item for item in repository.prompt_workspace()}
    for prompt_id, expected in PROMPT_ID_CONTENT.items():
        entry = prompts.get(prompt_id)
        if entry is None:
            print(f"{prompt_id} 不存在，跳过")
            continue
        published = next(
            (item for item in entry["versions"] if item["status"] == "published"), None
        )
        if published is None:
            print(f"{prompt_id} 无已发布版本，跳过")
            continue
        if published["content"].strip() == expected.strip():
            print(f"{prompt_id} 已是最新内容，无需迁移")
            continue
        version_id = repository.create_prompt_draft(
            prompt_id, expected, "系统升级：大脑自主研究能力（agent_calls / 跨域判断）"
        )
        repository.publish_prompt_version(version_id)
        print(f"{prompt_id} 已升级：{version_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
