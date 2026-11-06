# 시세표 이미지 판독 규칙 (클로드 앱 루틴용)

아래 규칙은 API 판독기(vision_api_reader.PROMPT)와 **같은 원문**이다. 이미지 1장(=manifest 1항목)을
읽고 결과를 `/tmp/sise_batch/results/<idx>.json` 에 아래 스키마의 JSON 객체 **하나**로 저장한다.

- 항목의 `pieces` 가 2개 이상이면: 세로로 긴 시세표를 위→아래 순서로 나눈 조각이다. 열 헤더는 첫
  조각에 있고 조각 경계에 겹침(150px)이 있으니 **중복 행은 한 번만** 추출한다.
- 시세표가 아니면(매장 사진·행사 포스터·조건 안내문) `{"is_price_table": false, "board_date": null, "rows": []}`.
- 값 단위·부호·열 대응·제외 대상 등은 아래 규칙을 그대로 따른다. 임의 추정 금지, 확신 없는 셀은 confidence 0.5 미만.

---

한국 휴대폰 성지 매장의 카카오채널 게시 이미지다. 시세표라면 표를 판독해 휴대폰 단가 행을 추출하라.

## 1단계: 표 구조 먼저 파악하라 (추출 전에)
- 열 헤더: 통신사(SK/KT/LG) × 가입유형(번호이동=MNP/기기변경=기변/신규) 구조가 일반적. 열 순서를 왼쪽부터 정확히 기억하라.
- 행 헤더: 한 모델이 여러 가격 줄을 가질 수 있다 — "현금가/현완가/현금완납가" 줄이 **기본 단가**다(add_condition=null). "이벤트가/페스티벌 적용가/추가페이백 적용가/OO적용가" 같은 조건부 줄도 별도 행으로 추출하되 **add_condition에 그 줄의 이름을 그대로 표기**하라. 할부원금·월할부금 줄은 넣지 마라.
  예시 — 모델 S26 아래 두 줄 "현금가 23 / 바페적용가 13"이면 두 행을 출력:
    {cash_price: 230000, add_condition: null}  ← 현금가 줄
    {cash_price: 130000, add_condition: "바페적용가"}  ← 조건부 줄
  **주의**: 매장 배너·상호의 행사명(페스티벌 등)이나 '최저가/특가' 같은 홍보 문구를 근거로 일반 행에 조건을 붙이지 마라. 행 단위 조건은 그 값의 '행 라벨'이나 셀 주석에 명시된 경우에만. 현금가 줄까지 전부 행사명으로 표기하는 것은 오류다.
- **표 전체 구매 조건**(행사명과 다름): 표 제목·상단 배너·하단 주석에 "인터넷+TV 동시 가입시", "결합 기준가", "제휴카드 할인 포함", "부가서비스 가입 적용된 조건"처럼 **구매 조건**이 표 전체에 걸려 있으면, 모든 행의 add_condition 에 그 조건을 기록하라(행별 조건이 따로 있으면 "표조건; 행조건" 순으로 이어 쓴다). 이 조건이 빠지면 결합 조건가가 일반 단가로 집계되는 오류가 난다.
- 섹션 헤더(요금제): 상단 요금제 헤더는 아래 모든 행에 상속된다. 중간에 "선택약정/선약/저가요금제/중가기종" 섹션이 새 요금제 헤더와 함께 나오면 그 섹션부터 새 요금제·contract_type='선약'으로 교체하라.

## 2단계: 셀 추출 규칙
1. 값 단위는 대부분 '만원' — cash_price는 원 단위 정수로(25 → 250000, -14 → -140000). 콤마 원단위(263,000)는 그대로.
2. 음수(빨간 글씨 포함) = 차비(페이백). 부호 그대로 유지.
3. **열-값 대응을 엄격히**: 각 행에서 값을 왼쪽부터 열 헤더 순서대로 1:1 대응시켜라. 'X', '별도문의', '품절', 검게 가려진 칸은 그 셀만 건너뛰고(행 생략), **다음 값을 절대 앞 열로 당겨 채우지 마라**. 값 개수가 열 수와 안 맞으면 해당 모델 행 전체 confidence를 0.5 미만으로.
4. 저가/중가/키즈폰 섹션의 휴대폰(갤럭시 A·버디·점프·퀀텀·와이드, 포켓몬폰 등)도 추출하라 — 해당 섹션의 요금제·선약 표기를 적용.
5. **제외 대상**: 스마트워치·태블릿·버즈·유심단독(기기 그대로 번호이동)·공신폰·인터넷/TV 상품 행, 그리고 상품권/사은품/캐시백 '혜택 금액'.
6. '결합', '인터넷+TV', '제휴카드', '온누리', '체감가' 조건이 붙은 값은 add_condition에 해당 키워드를 반드시 포함.
7. 월청구액 형식이면: 현금완납가 ≈ (월청구액 − 요금제 정가) × 약정개월수(미표기 24). estimated=true, add_condition='월청구추정'.
8. 'NNN요금제'는 정가 NNN,000원 (예: 109요금제 → plan_fee 109000).

## 3단계: 자체 검증 후 출력
- 출력 전에 표에서 무작위 3개 행을 골라 열 헤더와 값 대응을 다시 확인하라. 인접 열 값이 밀려 들어간 행이 없는지 점검하라.
- 같은 모델·통신사에서 번호이동가가 기변가보다 높은 행이 절반을 넘으면 열 대응이 뒤집힌 것이다 — 다시 대조하라(단, 개별 역전은 실제로 존재할 수 있음).
- 시세표가 아니면(매장 사진, 행사 포스터, 조건 안내문) is_price_table=false, rows=[].
- 확신 없는 셀은 confidence를 낮게(0.5 미만). 임의 추정 금지.

게시글 텍스트 컨텍스트(시세표 보는 법 등)는 session_manifest.json 의 각 항목 `context` 에 있다.


---

## 결과 JSON 스키마 (반드시 준수 — 키 누락/추가 금지, nullable 은 null)

```json
{
  "type": "object",
  "properties": {
    "is_price_table": {
      "type": "boolean",
      "description": "이 이미지가 휴대폰 단가/시세표인지 (매장사진·행사포스터·조건안내는 false)"
    },
    "board_date": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "description": "시세표에 표기된 기준일 YYYY-MM-DD (없으면 null)"
    },
    "rows": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "model_name": {
            "type": "string",
            "description": "정규화 모델명 — 영문 공식 표기로: 플러스→+, 울트라→Ultra, 프로맥스→Pro Max, 에어→Air (예: Galaxy S26+, Galaxy S26 Ultra, iPhone 17 Pro Max, iPhone Air, Galaxy Z Flip 7, Galaxy A17)"
          },
          "storage_gb": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ]
          },
          "carrier": {
            "anyOf": [
              {
                "type": "string",
                "enum": [
                  "SKT",
                  "KT",
                  "LGU+",
                  "알뜰"
                ]
              },
              {
                "type": "null"
              }
            ]
          },
          "subscription_type": {
            "anyOf": [
              {
                "type": "string",
                "enum": [
                  "MNP",
                  "기변",
                  "신규"
                ]
              },
              {
                "type": "null"
              }
            ]
          },
          "contract_type": {
            "anyOf": [
              {
                "type": "string",
                "enum": [
                  "공시",
                  "선약",
                  "자급"
                ]
              },
              {
                "type": "null"
              }
            ]
          },
          "cash_price": {
            "type": "integer",
            "description": "현금완납가, 원 단위 정수. 만원 표기는 ×10000. 음수 = 차비(페이백) 지급"
          },
          "plan_name": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ]
          },
          "plan_fee": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "description": "요금제 월정액 정가(원)"
          },
          "estimated": {
            "type": "boolean",
            "description": "월청구 공식 등으로 추정한 값이면 true"
          },
          "add_condition": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "description": "부가 조건. 결합/제휴카드/온누리 체감가 행은 반드시 해당 키워드 포함"
          },
          "confidence": {
            "type": "number",
            "description": "0~1"
          }
        },
        "required": [
          "model_name",
          "storage_gb",
          "carrier",
          "subscription_type",
          "contract_type",
          "cash_price",
          "plan_name",
          "plan_fee",
          "estimated",
          "add_condition",
          "confidence"
        ],
        "additionalProperties": false
      }
    }
  },
  "required": [
    "is_price_table",
    "board_date",
    "rows"
  ],
  "additionalProperties": false
}
```

예시:
```json
{"is_price_table": true, "board_date": "2026-09-07",
 "rows": [{"model_name": "Galaxy Z Fold 8", "storage_gb": 256, "carrier": "SKT", "subscription_type": "MNP",
           "contract_type": "공시", "cash_price": 230000, "plan_name": "5GX프라임", "plan_fee": 89000,
           "estimated": false, "add_condition": null, "confidence": 0.9}]}
```
