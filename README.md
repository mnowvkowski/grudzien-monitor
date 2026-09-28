# Monitor Groszku Grudzień

Sprawdza 26 MJ i 29 MJ dla wariantu **Gospodarstwo domowe**. GitHub Actions uruchamia monitor co 5 minut (minuty 2, 7, 12…). Wysyła push przez ntfy przy zmianie dostępności, również gdy produkt jest dostępny przy pierwszym sprawdzeniu. Pierwszy odczyt „niedostępny” pozostaje cichy.

## Uruchomienie

1. Wgraj pliki do domyślnej gałęzi repozytorium GitHub i włącz Actions.
2. Dodaj sekret repozytorium `NTFY_TOPIC` zawierający nazwę prywatnie wybranego, długiego losowego tematu ntfy (1–64 znaków: litery ASCII, cyfry, `_`, `-`). Tematu nie umieszczaj w kodzie, logach ani pliku stanu.
3. W aplikacji ntfy zasubskrybuj ten sam temat na `https://ntfy.sh` i włącz powiadomienia.
4. W Actions uruchom ręcznie workflow z `test_notification` zaznaczonym, aby sprawdzić dostarczenie. Test nie pobiera produktów ani nie zmienia ich stanu.

Monitor używa systemowego Python 3, bez dodatkowych zależności. Token Actions musi mieć prawo zapisu do domyślnej gałęzi.

```sh
python3 -m unittest -v
python3 monitor.py --dry-run
# Wymaga wcześniej ustawionego sekretu w środowisku:
python3 monitor.py --test-notification
python3 monitor.py
```

## Odczyt i stan

Źródłem są dane `data-product_variations` z właściwego formularza WooCommerce: produkt 963 / wariant 2391 oraz produkt 960 / wariant 2398. Dostępność wymaga prawdziwych wartości logicznych `is_in_stock` i `is_purchasable`, a także `variation_is_active` i `variation_is_visible`, jeśli występują. Inny wariant lub nieznany format oznacza błąd, nie brak towaru.

`state.json` powstaje automatycznie na domyślnej gałęzi. Zapis następuje przy pierwszym poprawnym odczycie, zmianie, błędzie/odzyskaniu sprawności oraz raz na datę UTC przy kolejnym poprawnym odczycie. `observed_at` to rzeczywisty czas odczytu zapisanego w danym wpisie, nie czas każdego uruchomienia. Bez zmian dostępności i błędów nie powstaje 288 commitów dziennie.

Błąd pobrania lub parsowania zachowuje ostatni stan produktu; drugi produkt jest sprawdzany niezależnie. Monitor wysyła jeden alert na epizod błędu danego produktu. Jeśli alert nie został przyjęty przez ntfy, próbuje ponownie przy następnym uruchomieniu. Zmiana dostępności zostaje zapisana dopiero po potwierdzeniu przyjęcia powiadomienia. Nieudane uruchomienie pozostaje czerwone w Actions, ale poprawny stan drugiego produktu jest zapisywany.

`--dry-run` pobiera i rozpoznaje produkty bez powiadomień oraz odczytu/zapisu stanu. Żądania mają limit czasu 12 sekund; odczyt strony ma maksymalnie dwie próby. POST ntfy nie jest ponawiany od razu. Po utracie potwierdzenia ntfy lub nieudanym zapisie/push stanu kolejne uruchomienie może powtórzyć powiadomienie.

GitHub może opóźnić lub pominąć uruchomienie harmonogramu; nie gwarantuje ścisłego interwału 5 minut. W publicznym repozytorium bez aktywności harmonogram może zostać wyłączony po 60 dniach. Losowy temat ntfy jest sekretem dostępu: każda osoba znająca jego nazwę może go subskrybować lub na niego publikować.
