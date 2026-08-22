<h1 align="center" style="font-size:4em;">TimeKeeper</h1>
<p align="center" style="font-size:1.5em;"><b>- персональная система контроля рабочего процесса</b></p>

<p align="center">
  <img src="static/media/lockup-animated.gif">
</p>



## Возможности:
> - Запуск и остановка отсчёта времени по каждой задаче помогают отслеживать результаты и время, затраченное на их выполнение.
> - Отдельный блок для фиксации актуальных задач позволяет всегда держать в фокусе самые важные из них.
> - Результаты работы удобно сохранять в отчётах: в форматах PNG и TXT.
> - Система Kanban упрощает учёт рабочих задач, распределяя нагрузку при работе над глобальными проектами или при планировании работы в команде.


## Установка

```bash
git clone https://github.com/codemed7-git/TimeKeeper.git
cd TimeKeeper

python -m venv venv

venv\Scripts\activate

source venv/bin/activate

pip install -r requirements.txt

python app.py
```

Откройте браузер и перейдите по адресу: http://127.0.0.1:7777/

База данных `timekeeper.db` создаётся автоматически при первом запуске.