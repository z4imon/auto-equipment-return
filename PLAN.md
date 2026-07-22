## World of Tanks Auto Equipment Return Mod

### Goal of the mod
- Das World of Tanks Abonnement "World of Tanks Plus" und "World of Tanks Plus Pro" erlauben es Equipment ohne Kosten auszubauen
- Dies gilt für
    - Standard Equipment
    - Erbeutetes Equipment (rosa)
    - Experimentelles Equipment Stufe 1
- Dies gilt nicht für
    - Verbessertes Equipment
    - Experimentelles Equipment Stufe 2 und 3
- Das erlaubt es einem das Erbeutete Equipment automatisch zwischen den ausgewählten Panzern hin und her zu bewegen
- Immer wenn ein neuer Panzer ausgewählt wird, dann soll das gespeicherte Equipment in den Panzer eingebaut werden
    - Wenn das Equipment nicht im Lager ist, dann soll es aus einem Fahrzeug ausgebaut werden in dem es eingebaut ist (darf nicht im Gefecht sein)

### Was muss alles implementiert werden
- Ein weiterer Button soll neben dem toggle visibility Button der Tank Info in Hangar Mod eingefügt werden (bzw rechts von den standard buttons)
- Beim klicken dieses Buttons soll ein popover geöffnet werden, dort sollen die gespeicherten Equipment Sets (2 pro Panzer) angezeigt werden.
    - Zusätzlich eine Liste von klickbaren buttons vertikal übereinander
        - Speichere (überschreibe) Equipment Set 1  
        - Speichere (überschreibe) Equipment Set 2
        - Speichere (überschreibe) beide Equipment Sets
- Wenn ein Panzer in der Garage ausgewählt wird, dann soll das gespeicherte Equipment geladen werden und automatisch eingebaut werden