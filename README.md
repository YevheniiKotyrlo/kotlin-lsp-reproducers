# kotlin-lsp reproducers

`lsp_probe.py` drives `intellij-server --stdio` with Python's standard library alone. It initializes the server with the folders named, waits until every work-done progress the server began has ended, can add folders with `workspace/didChangeWorkspaceFolders`, then asks hover and go-to-definition at the positions named (zero-based lines and characters, as the protocol counts them). It prints the server's workspace capability, each answer and the server log's handler-not-found lines, and keeps a copy of the server log beside its `--record` file.

The fixtures are Gradle builds:

- `fixtures/first` and `fixtures/second` are Kotlin/JVM builds; `second`'s `Main.kt` calls `greet`, which its `Greeter.kt` declares.
- `fixtures/composite/library` is a Kotlin/JVM build that `fixtures/composite/app` includes with `includeBuild` and a `dependencySubstitution`.
- `fixtures/android-composite/library` is an Android library build that the Android application `fixtures/android-composite/app` includes and substitutes, beside `:local`, an Android library of its own build.

The Kotlin/JVM fixtures pin Gradle 8.14.3 and Kotlin 2.2.10; the Android ones Gradle 8.14.3, the Android Gradle plugin 8.12.0, Kotlin 2.1.20 and compile SDK 36, and need `ANDROID_HOME` set. `SERVER` below is the release's `bin/intellij-server` (`intellij-server.exe` on Windows).

## Kotlin/kotlin-lsp#296 — an added workspace folder is never imported

```sh
python lsp_probe.py --server "$SERVER" --folder fixtures/second --definition fixtures/second/src/main/kotlin/Main.kt:1:12 --hover fixtures/second/src/main/kotlin/Main.kt:1:12 --record runs/296-control.json
python lsp_probe.py --server "$SERVER" --folder fixtures/first --add fixtures/second --grace 120 --definition fixtures/second/src/main/kotlin/Main.kt:1:12 --hover fixtures/second/src/main/kotlin/Main.kt:1:12 --record runs/296-added.json
```

The first run answers the definition with `Greeter.kt`; the second, where `second` is added after `first`'s import, begins no import and answers nothing.

## Kotlin/kotlin-lsp#297 — an Android library from an included build resolves nothing

```sh
python lsp_probe.py --server "$SERVER" --folder fixtures/android-composite/app --definition fixtures/android-composite/app/src/main/kotlin/com/example/app/App.kt:5:25 --definition fixtures/android-composite/app/src/main/kotlin/com/example/app/App.kt:5:43 --hover fixtures/android-composite/app/src/main/kotlin/com/example/app/App.kt:5:25 --hover fixtures/android-composite/app/src/main/kotlin/com/example/app/App.kt:5:43 --record runs/297.json
```

`Local` (5:43), from the app's own `:local`, resolves; `Library` (5:25), from the included build, does not.

## Kotlin/kotlin-lsp#298 — an Android module that has not been built gets no Java compiler arguments

```sh
python lsp_probe.py --server "$SERVER" --folder fixtures/android-composite/app --record runs/298.json
grep "Unable to resolve all compiler arguments" runs/298.server.log
```

On a fresh clone, with nothing built, the server log holds the error once for each variant of each Android module.
