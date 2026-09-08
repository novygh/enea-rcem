# Enea RCEm

Niestandardowa integracja dla Home Assistanta przeznaczona do rozliczeń prosumenckich z **Enea / Enea Operator G11** z wykorzystaniem miesięcznych wartości **PSE RCEm**.

**Aktualna stabilna wersja: 1.1.1**

> Projekt służy do odtwarzania i monitorowania logiki rozliczeń w Home Assistant. Wartości taryf i końcowe rozliczenia należy zawsze porównywać z własną umową i fakturą.

## Zakres

Enea RCEm jest przeznaczona dla prosumentów rozliczanych według **miesięcznego RCEm**, a nie godzinowego RCE. Korzysta z istniejących w Home Assistant skumulowanych liczników importu i eksportu energii oraz lokalnie buduje sensory związane z rozliczeniem.

Integracja:

- wykonuje godzinowe bilansowanie importu i eksportu,
- stosuje niezależną kalibrację importu i eksportu po bilansowaniu,
- pobiera oficjalne miesięczne wartości RCEm z PSE,
- wykrywa późniejsze korekty RCEm publikowane przez PSE,
- przypisuje rekompensatę za eksport do miesiąca, w którym energia została oddana do sieci,
- udostępnia ostatni w pełni rozliczony miesiąc wraz z kosztem importu i rekompensatą eksportu do użycia na dashboardach,
- udostępnia dzienną serię kosztu i wartości eksportu dla tego rozliczonego miesiąca,
- udostępnia sensory miesięcznego bilansu energii i finansów ze znakiem,
- stosuje historyczne zasady współczynnika prosumenckiego,
- oblicza koszt brutto importu na podstawie konfigurowalnych stawek Enea / Enea Operator i VAT,
- nalicza stałe opłaty miesięczne proporcjonalnie w ciągu miesiąca,
- odtwarza wiarygodne przyrosty liczników skumulowanych po restartach Home Assistanta,
- rozdziela odzyskany wielogodzinny przyrost pomiędzy brakujące przedziały godzinowe zamiast gubić energię,
- zwiększa diagnostykę **Luki danych** wyłącznie wtedy, gdy brakuje wiarygodnej wartości bazowej któregoś źródła,
- rekonstruuje depozyt prosumencki ze statystyk Recorder,
- przechowuje trwałe liczniki robocze w magazynie danych Home Assistanta bez dodatkowych helperów.

## Sensory

### Energia

- **Import zbilansowany** — skumulowany import po bilansowaniu godzinowym; obliczenia wykonywane są w kWh, sugerowane wyświetlanie w MWh z trzema miejscami po przecinku.
- **Eksport zbilansowany** — skumulowany eksport po bilansowaniu godzinowym; obliczenia wykonywane są w kWh, sugerowane wyświetlanie w MWh z trzema miejscami po przecinku.
- **Bilans energii bieżącego miesiąca** — miesięczny bilans sieci ze znakiem w kWh, liczony jako import minus eksport. Wartość ujemna oznacza przewagę eksportu, dodatnia przewagę importu.

### Ceny i rozliczenia

- **PSE RCEm** — najnowsza opublikowana miesięczna wartość RCEm.
- **PSE RCEm Prosument** — RCEm z aktualnie obowiązującym współczynnikiem prosumenckim.
- **Koszt importu** — skumulowany koszt brutto importu.
- **Rekompensata eksportu** — skumulowana wartość rozliczonego eksportu dla miesięcy z opublikowanym RCEm.
- **Bilans ostatniego rozliczonego miesiąca** — wynik finansowy ze znakiem dla najnowszego zamkniętego miesiąca, dla którego dostępne są RCEm i dane rozliczeniowe Recorder; liczony jako koszt importu minus rekompensata eksportu. Wartość dodatnia oznacza koszt netto, ujemna oznacza, że wartość eksportu przekroczyła koszt importu.
- **Szacowana rekompensata eksportu bieżącego miesiąca** — szacunek na podstawie najnowszej dostępnej wartości RCEm do czasu oficjalnej publikacji RCEm dla bieżącego miesiąca.

Sensor **Rekompensata eksportu** udostępnia również atrybuty przeznaczone do dashboardów dla najnowszego zamkniętego miesiąca, dla którego istnieją zarówno oficjalna publikacja RCEm, jak i dane rozliczeniowe Recorder:

- `last_settled_month`,
- `last_settled_import_cost_pln`,
- `last_settled_export_compensation_pln`,
- `last_settled_daily_month`,
- `last_settled_daily`.

`last_settled_daily` zawiera po jednej pozycji dla każdego dnia kalendarzowego wybranego miesiąca:

- `date`,
- `import_cost_pln`,
- `export_compensation_pln`,
- `export_kwh`.

Dzienna wartość eksportu jest rekonstruowana na podstawie zbilansowanego eksportu z danego dnia oraz oficjalnego RCEm/współczynnika dla wybranego miesiąca rozliczeniowego. Dzięki temu miesięczna korekta RCEm nie jest przypisywana w całości do pojedynczego dnia.

Jeżeli bezpośrednio poprzedzający miesiąc kalendarzowy nie jest jeszcze rozliczony, integracja automatycznie wybiera najnowszy wcześniejszy miesiąc, który jest już rozliczony.

### Depozyt prosumencki

- **Stan depozytu prosumenckiego** — aktualnie dostępny stan depozytu.
- **Depozyt przypisany w tym miesiącu** — wartość przypisana do depozytu w bieżącym miesiącu z poprzedniego miesiąca rozliczeniowego.
- **Depozyt wykorzystany w tym miesiącu** — część depozytu już wykorzystana na pokrycie zakupu energii czynnej w bieżącym miesiącu.
- **Energia czynna do zapłaty w tym miesiącu** — kwota za energię czynną pozostała do zapłaty po wykorzystaniu depozytu.

### Diagnostyka

- **Energia przed PV** — informacyjna stała: 1032 kWh dla pełnego okresu fakturowego od 2024-03-20 do 2024-06-11.
- **Koszt przed PV** — informacyjna stała: 1123,66 PLN dla tego samego pełnego okresu fakturowego.
- **Luki danych** — licznik zwiększany tylko wtedy, gdy co najmniej jedna bazowa wartość skumulowanego licznika źródłowego jest brakująca lub niewiarygodna. Zwykły restart, po którym możliwe jest odzyskanie przyrostów skumulowanych, nie jest traktowany jako luka.

Sensory referencyjne sprzed PV są wyłącznie informacyjne. Nie są uwzględniane w obliczeniach RCEm, skumulowanych licznikach okresu PV ani statystykach Energy Dashboard. Historyczne rozliczenia prosumenckie zaczynają się od 2024-06-12.

## Energy Dashboard

Dla konfiguracji sieci używaj własnych statystyk integracji:

- import z sieci: **Import zbilansowany**,
- eksport do sieci: **Eksport zbilansowany**,
- koszt importu: **Koszt importu**,
- rekompensata eksportu: **Rekompensata eksportu**.

Nie podłączaj osobnej encji bieżącej ceny, jeżeli używasz powyższych skumulowanych sensorów kosztu i rekompensaty.

Miesięczne sensory bilansu ze znakiem są pomocniczymi migawkami do dashboardów i nie zastępują statystyk skumulowanych używanych przez Energy Dashboard.

## Instalacja przez HACS

1. Otwórz **HACS**.
2. Otwórz menu z trzema kropkami i wybierz **Niestandardowe repozytoria**.
3. Dodaj `https://github.com/novygh/enea-rcem` jako typ **Integracja**.
4. Pobierz **Enea RCEm**.
5. Uruchom ponownie Home Assistanta.
6. Przejdź do **Ustawienia → Urządzenia i usługi → Dodaj integrację → Enea RCEm**.
7. Wybierz istniejące skumulowane sensory importu i eksportu energii z sieci.

## Konfiguracja

Formularz konfiguracji zawiera stawki Enea / Enea Operator G11 używane przez integrację. Wartości można później zmieniać przez akcję **Konfiguruj** integracji.

W szczególności porównaj z własną umową lub fakturą:

- cenę energii czynnej,
- opłatę handlową,
- składniki taryfy dystrybucyjnej i opłat ustawowych,
- VAT,
- wartości kalibracji importu/eksportu, jeżeli są używane.

Kalibracja jest stosowana **po godzinowym bilansowaniu**, dlatego zmiana korekty importu nie wpływa na bilansowanie eksportu i odwrotnie.

## Restart i odzyskiwanie brakujących danych

Fizyczne sensory źródłowe powinny być licznikami skumulowanymi, które zachowują swoje wartości całkowite podczas wyłączenia Home Assistanta.

Jeżeli Home Assistant uruchomi się ponownie po przekroczeniu jednej lub kilku granic godzin, a obie wartości bazowe źródeł pozostaną wiarygodne, Enea RCEm:

1. odczytuje nowe wartości skumulowane,
2. oblicza brakujący przyrost importu i eksportu,
3. rozdziela ten przyrost pomiędzy brakujące przedziały godzinowe,
4. kontynuuje bilansowanie godzinowe,
5. zachowuje całkowitą odzyskaną liczbę kWh,
6. **nie** zwiększa sensora `Luki danych`.

Jeżeli brakuje wartości bazowej albo licznik źródłowy niespodziewanie maleje/resetuje się, integracja ustala nową bazę dla danego źródła, zachowuje wiarygodny przyrost z drugiego źródła i zwiększa `Luki danych`.

## Korekty RCEm

PSE może publikować poprawione wartości RCEm po pierwotnej publikacji. Integracja okresowo uzgadnia długoterminowe statystyki Recorder, dzięki czemu późniejsza korekta jest przypisywana do pierwotnego miesiąca eksportu zamiast do miesiąca, w którym opublikowano korektę.

Obliczenia Recorder jawnie żądają energii w **kWh**, dlatego zmiana jednostki wyświetlania sensorów energii na MWh nie wpływa na matematykę rozliczeń.

## Depozyt prosumencki

Model depozytu jest rekonstruowany na podstawie miesięcznych statystyk Recorder. Wartość eksportu jest przypisywana do następnego miesiąca rozliczeniowego, a partie depozytu są zużywane od najstarszej zgodnie z zaimplementowanymi zasadami rozliczeń.

Sensory depozytu są bieżącymi migawkami. Skumulowana historia rozliczeń pozostaje w statystykach Recorder oraz w skumulowanych sensorach importu, eksportu, kosztu i rekompensaty.

## Dane historyczne i narzędzia odzyskiwania

Wersja 1.1.1 **nie** tworzy automatycznie historii sprzed instalacji. Zaawansowane narzędzia migracyjne i diagnostyczne znajdują się w katalogu `tools/` i są przeznaczone dla instalacji, w których istnieją wiarygodne historyczne statystyki skumulowanych liczników.

Produkcyjna integracja nie udostępnia jednorazowych usług naprawiających bazę danych. Tymczasowe usługi użyte do naprawy zdiagnozowanej granicy migracji z 2026-08-25 zostały usunięte po weryfikacji; ich kod pozostaje dostępny w historii Git. Do przyszłych analiz służy narzędzie tylko do odczytu `tools/recorder_statistics_audit.py` sprawdzające ciągłość statystyk Recorder.

Historyczne zapisy do Recorder należy traktować jako operację zaawansowaną: przed migracją wykonaj kopię zapasową Home Assistanta i po migracji sprawdź wynikowe statystyki długoterminowe. Przed użyciem narzędzi migracyjnych przeczytaj `tools/README.md`.

## Źródła danych

- lokalne skumulowane sensory importu i eksportu energii w Home Assistant,
- miesięczne publikacje RCEm PSE,
- skonfigurowane stawki sprzedawcy Enea / Enea Operator,
- długoterminowe statystyki Home Assistant Recorder używane do uzgodnień, szczegółów dziennych i rekonstrukcji depozytu.

## Zastrzeżenie

Projekt jest niezależny i nie jest powiązany z Enea S.A., Enea Operator, PSE ani Home Assistant. Nie zastępuje faktury za energię ani oficjalnego rozliczenia.
