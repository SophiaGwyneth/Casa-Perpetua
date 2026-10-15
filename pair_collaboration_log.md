# Casa Perpetua Virtual Waiter - Pair Collaboration Log
**Date:** October 10, 2026
**Class:** AI Lab - Lesson 6 (Conversational State Machines)

## 1. Role Rotation & Task Distribution

| Phase | Duration | Driver (Keyboard/Code) | Navigator (Logic/Architecture) | Task Focus |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 1: DB & Tool Binding** | 45m | Student A | Student B | `db_tools.py` schema declarations including 15 distinct categories and CRUD execution. |
| **Phase 2: Visual Kiosk Integrations**| 50m | Student A | Student B | Implementing dynamic HTML rendering for food images inside `virtual_waiter_prototype.py`. |
| *Role Rotation* | - | *Roles Swapped* | *Roles Swapped* | - |
| **Phase 3: FSM & Agent Loop** | 45m | Student B | Student A | Qwen 2.5 JSON parsing and multi-turn state transitions enforcing menu guardrails. |
| **Phase 4: Manual Overrides** | 30m | Student B | Student A | Adding `api_manual_override` for instant Cart (+/-) adjustments. |

## 2. Key Architectural Decisions
1. **Visual Kiosk UI:** We designed a dynamic scrollable category ribbon that interacts with `get_menu_by_category`. This allows the user to browse 15 global categories visually (including newly added Chinese and Thai food). 
2. **Human-in-the-Loop Cart Overrides:** We created a dedicated REST endpoint (`/api/manual_override`) that bypasses Qwen 2.5. This allows customers to instantly use the interactive (+ / -) buttons on the Kiosk UI to adjust item quantities without waiting for AI processing.