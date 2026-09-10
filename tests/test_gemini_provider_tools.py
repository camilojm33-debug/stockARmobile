from services.ai_agent.providers.gemini import GeminiProvider


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


def test_gemini_extracts_all_function_calls_from_candidate_parts():
    response = Obj(
        candidates=[
            Obj(
                content=Obj(
                    parts=[
                        Obj(function_call=Obj(name="buscar_producto", args={"query": "cafe"})),
                        Obj(function_call=Obj(name="consultar_stock", args={"product_id": 7})),
                        Obj(text="texto opcional"),
                    ]
                )
            )
        ]
    )

    calls = GeminiProvider._tool_calls(response)

    assert [call["name"] for call in calls] == ["buscar_producto", "consultar_stock"]
    assert calls[0]["arguments"] == {"query": "cafe"}
    assert calls[1]["arguments"] == {"product_id": 7}


def test_gemini_tool_calls_are_empty_without_function_call_parts():
    response = Obj(candidates=[Obj(content=Obj(parts=[Obj(text="respuesta final")]))])
    assert GeminiProvider._tool_calls(response) == []
