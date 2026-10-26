@echo off
"%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" /nologo /r:Caesar.dll /r:Newtonsoft.Json.dll /out:CbfComParams.exe CbfComParams.cs
