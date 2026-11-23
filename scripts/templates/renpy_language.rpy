init 999 python hide:
    import renpy as engine

    # Ren'Py serializes preferences.__dict__. Keep this patch's language selection
    # in the running session so other preferences and save progress persist normally.
    cls = type(engine.game.preferences)
    if "language" not in vars(cls):
        def read_language(obj, engine=engine):
            if obj is engine.game.preferences and "hanhua_session_language" in engine.session:
                return engine.session["hanhua_session_language"]
            return vars(obj).get("language")

        def set_language(obj, value, engine=engine):
            if obj is engine.game.preferences:
                engine.session["hanhua_session_language"] = value
            else:
                vars(obj)["language"] = value

        cls.language = property(read_language, set_language)
    engine.config.language = "schinese"

translate schinese python:
    gui.text_font = gui.name_text_font = gui.interface_text_font = gui.button_text_font = gui.choice_button_text_font = "hanhua-font/NotoSansSC.ttf"

translate schinese style default:
    font "hanhua-font/NotoSansSC.ttf"
