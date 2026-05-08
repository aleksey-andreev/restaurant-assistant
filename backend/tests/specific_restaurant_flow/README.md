# Specific Restaurant Flow Tests

Тесты в этой папке покрывают новый клиентский путь: бронирование столика в **конкретном** ресторане.

## Что покрыто

| Кейс | Файл | Проверка |
|---|---|---|
| Intent detection | `test_specific_booking_intent.py` | Выбор `specific_restaurant` для прямой брони и отсутствие ложного срабатывания для обычного поиска |
| Resolver: resolved | `test_specific_restaurant_resolver.py` | Один сильный матч -> `status=resolved`, выбранный кандидат |
| Resolver: ambiguous | `test_specific_restaurant_resolver.py` | Несколько совпадений -> `status=ambiguous`, список кандидатов |
| Resolver: not_found | `test_specific_restaurant_resolver.py` | Нет релевантных карточек -> `status=not_found` |
| Dialog API: ambiguous flow | `test_dialog_specific_ambiguous_flow.py` | Карточки при неоднозначности + выбор через `select_booking_candidate` |
| Dialog API: resolved flow | `test_dialog_specific_resolved_flow.py` | Сразу выбранный ресторан и переход к форме бронирования |
| Dialog API: not_found flow | `test_dialog_specific_not_found_flow.py` | Запрос уточнений, без активной брони и без выбранного кандидата |

## Запуск

Из корня репозитория:

```bash
python3 -m unittest backend/tests/specific_restaurant_flow/test_specific_booking_intent.py \
  backend/tests/specific_restaurant_flow/test_specific_restaurant_resolver.py \
  backend/tests/specific_restaurant_flow/test_dialog_specific_ambiguous_flow.py \
  backend/tests/specific_restaurant_flow/test_dialog_specific_resolved_flow.py \
  backend/tests/specific_restaurant_flow/test_dialog_specific_not_found_flow.py
```

Или вся папка:

```bash
python3 -m unittest discover -s backend/tests/specific_restaurant_flow -p "test_*.py"
```
