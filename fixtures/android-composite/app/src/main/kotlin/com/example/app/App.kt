package com.example.app

import com.example.library.Library
import com.example.local.Local

fun greeting(): String = Library.hello() + Local.hello()
