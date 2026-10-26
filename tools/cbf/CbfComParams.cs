// Prints every ECU interface subtype's communication parameters from a CBF (CP_REQUEST_CANIDENTIFIER,
// CP_RESPONSE_CANIDENTIFIER, CP_BAUDRATE, ...). Replaces CaesarSuite's Trafo, which is no longer shipped.
// Needs Caesar.dll + Newtonsoft.Json.dll (CaesarSuite_2022-04-28.zip, .NET Framework 4.6) beside it.
// C# 5 on purpose: builds with the in-box csc.exe, see build.cmd.
using System;
using System.IO;
using Caesar;

class CbfComParams
{
    static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: CbfComParams <file.cbf> [ecu-qualifier]");
            return 2;
        }
        CaesarContainer container = new CaesarContainer(File.ReadAllBytes(args[0]));
        foreach (ECU ecu in container.CaesarECUs)
        {
            if (args.Length > 1 && !string.Equals(ecu.Qualifier, args[1], StringComparison.OrdinalIgnoreCase))
                continue;
            Console.WriteLine("ECU " + ecu.Qualifier);
            for (int i = 0; i < ecu.ECUInterfaces.Count; i++)
                Console.WriteLine("  interface[" + i + "] " + ecu.ECUInterfaces[i].Qualifier);
            foreach (ECUInterfaceSubtype st in ecu.ECUInterfaceSubtypes)
            {
                Console.WriteLine("  subtype " + st.Qualifier);
                foreach (ComParameter cp in st.CommunicationParameters)
                    Console.WriteLine("    " + cp.ParamName + " = " + cp.ComParamValue
                        + " (0x" + cp.ComParamValue.ToString("X") + ")  interface " + cp.ParentInterfaceIndex);
            }
        }
        return 0;
    }
}
