# AI Agent Runtime — production contract

The shared AgentRuntime supports deterministic, multi-turn tool execution for all tenant AI agents.

- Vendedor: product/customer lookup, stock, cart, and pending order preparation.
- Asistente: product/customer lookup, stock, counts, sales summaries, ranking, recent-sales gaps, and critical stock.
- Analista: sales comparison, rankings, recent-sales gaps, critical stock, customer counts, and inactive customers.
- Marketing: product/customer context, inactive customers, promotable products, and draft campaign preparation.

Tool execution is bounded to a finite number of rounds. The model must receive tool results before producing business claims, and campaign generation remains a draft/approval flow.
