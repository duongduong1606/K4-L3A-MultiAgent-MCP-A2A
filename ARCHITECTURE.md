# L3A Architecture Record

Tài liệu này mô tả workflow L3A đã triển khai. Nội dung chỉ ghi lại kiến trúc
phối hợp agent, cách dùng MCP evidence, quy tắc kiểm tra và khả năng tái lập.
Không ghi prompt bí mật, API key, secret hoặc chain-of-thought.

## 1. System Overview

```text
                      ┌──────────────────────────┐
                      │   Coordinator / Router   │
                      └─────────────┬────────────┘
                                    │ (Handoff)
     ┌──────────────────────────────┼──────────────────────────────┐
     ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                  [END OUTPUT]
```


## 2. Agent Ownership

| Actor | Input | Trách nhiệm | Output / handoff |
| --- | --- | --- | --- |
| Coordinator | Case JSON | Đọc `case_id`, `claimed_order_id`, claims và `policy_version`; phân việc cho agent phù hợp | Event `task_assigned` |
| Order / Item Agent | `case_id`, `order_id` | Lấy evidence chính thức về order và item | MCP envelope và `evidence_ref` |
| Payment Agent | `case_id`, `order_id` | Lấy evidence thanh toán/refund khi claim yêu cầu | MCP envelope và `evidence_ref` |
| Shipment Agent | `case_id`, `order_id` | Lấy evidence giao hàng cho claim giao trễ | MCP envelope và `evidence_ref` |
| Policy Agent | `case_id`, `policy_version` | Đọc policy rule chính thức cho issue đã chọn | Case status, refund amount, action, responsible parties |
| Verifier Agent | Draft output và evidence refs | Kiểm tra case id, evidence, schema-oriented invariants | Output dictionary cuối |

Quyền gọi tool được giới hạn theo vai trò:

| Actor | Tool được gọi |
| --- | --- |
| Order / Item Agent | `get_order`, `get_order_items` |
| Payment Agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| Shipment Agent | `get_shipment_summary` |
| Policy Agent | `get_policy` |
| Verifier Agent | Không gọi MCP tool |

## 3. A2A Protocol

Coordinator dùng giao thức A2A nội bộ đơn giản. Mỗi handoff có:

```text
case_id
target actor
order_id hoặc policy_version
observable trace event
```

`case_id` là khóa correlation cho mọi agent và mọi MCP call. Agent không dùng
evidence chéo case. Handoff đi một chiều và có giới hạn:

```text
coordinator -> specialist -> policy -> verifier
```

Không có vòng lặp agent đệ quy. Verifier chỉ finalize khi đã có ít nhất một
`evidence_ref` thật từ MCP.

## 4. Evidence Lifecycle

MCP evidence được lấy qua `EvidenceGateway.call`. Gateway validate MCP envelope
theo `mcp-evidence-response-v1.schema.json`.

Với mỗi tool call thành công:

1. Agent giữ nguyên `evidence_ref` do Gateway trả về.
2. Agent emit `tool_result_consumed`.
3. Output cuối chỉ cite các `evidence_ref` đã thu thập được.

Workflow không tự tạo, không sửa và không dùng lại `evidence_ref` từ case khác.

## 5. Failure Policy

| Failure | Retry? | Fallback | Trace event / code |
| --- | --- | --- | --- |
| MCP proxy/environment issue | Không retry trong workflow | Gateway dùng `trust_env=False` để tránh proxy môi trường sai | N/A |
| Optional MCP tool failure | Không | Tiếp tục nếu evidence phụ không lấy được | Không emit `tool_result_consumed` nếu không có evidence |
| Thiếu order id | Không | Raise `ValueError` vì không scope được case | N/A |
| Thiếu policy rule | Không | Dùng `needs_investigation`, refund 0, responsible party unknown | `policy_decided` |
| Draft output invalid | Không | Raise trước khi ghi output | Không emit `verification_completed` |

Stable run giới hạn optional MCP call theo claim topic để giảm rủi ro stream
timeout, nhưng vẫn lấy payment, refund và shipment evidence khi liên quan.

## 6. Verification Invariants

Trước khi return output, verifier kiểm tra:

- `output["case_id"]` bằng input `case_id`.
- Có ít nhất một `evidence_ref` thật từ MCP.
- Confidence nằm trong range của schema.
- Refund là số BRL không âm.
- Responsible parties và resolution actions lấy từ policy rule khi có.
- Entity lists chỉ scope trong order hiện tại.

CLI tiếp tục validate output theo `contracts/schemas/l3a-output-v2.schema.json`.

## 7. Reproducibility

Các lệnh dùng cho stable run:

```bash
day09 mcp-tools
day09 run
day09 validate
```

Kết quả validate:

```text
OK: 100 outputs / 1270 trace events
```

Model metadata và provider config được đọc từ `.env` và đóng gói qua submission
manifest. API key và secret chỉ nằm trong `.env`, không commit và không đưa vào
submission ZIP.
