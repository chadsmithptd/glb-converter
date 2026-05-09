#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  define NOMINMAX
#  include <windows.h>
#endif

#include <algorithm>
#include <cctype>
#include <cmath>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include <gp_Ax1.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>
#include <gp_XYZ.hxx>

#ifdef _WIN32
#  include <AIS_InteractiveContext.hxx>
#  include <AIS_Shape.hxx>
#  include <Aspect_DisplayConnection.hxx>
#  include <Graphic3d_TypeOfShadingModel.hxx>
#  include <Image_AlienPixMap.hxx>
#  include <OpenGl_GraphicDriver.hxx>
#  include <Quantity_Color.hxx>
#  include <V3d_TypeOfOrientation.hxx>
#  include <V3d_View.hxx>
#  include <V3d_Viewer.hxx>
#  include <WNT_WClass.hxx>
#  include <WNT_Window.hxx>
#  include <XCAFPrs_AISObject.hxx>
#endif

#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepTools.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <Interface_Static.hxx>
#include <Message_ProgressRange.hxx>
#include <Precision.hxx>
#include <RWGltf_CafWriter.hxx>
#include <Standard_Version.hxx>
#include <STEPCAFControl_Reader.hxx>
#include <STEPControl_Reader.hxx>
#include <Standard_Failure.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <TColStd_SequenceOfAsciiString.hxx>
#include <TCollection_AsciiString.hxx>
#include <TCollection_ExtendedString.hxx>
#include <TDF_LabelSequence.hxx>
#include <TDocStd_Document.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <XCAFApp_Application.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>


namespace
{
    struct CurvatureStats
    {
        std::size_t sampleCount = 0;
        double minValue = std::numeric_limits<double>::infinity();
        double maxValue = 0.0;
        double sum = 0.0;

        void add(double v)
        {
            if (!std::isfinite(v) || v < 0.0)
                return;
            minValue = std::min(minValue, v);
            maxValue = std::max(maxValue, v);
            sum += v;
            ++sampleCount;
        }

        double avg() const
        {
            return sampleCount ? sum / static_cast<double>(sampleCount) : 0.0;
        }
    };

    // Cylinder face axis + area, collected during the face loop for symmetry analysis.
    struct CylFaceAxisInfo
    {
        gp_Ax1 axis;
        double area = 0.0;
    };

    // Counts UV sample points by required tool size based on signed concave curvature.
    // MaxCurvature() > 0 means the curvature center is on the outward-normal side →
    // concave feature (hole, pocket, fillet) that constrains the tool radius.
    struct ToolAccessSamples
    {
        int large  = 0;  // concave radius > 0.25"  (tool dia > 0.5")
        int medium = 0;  // concave radius 0.125–0.25" (tool dia 0.25–0.5")
        int small  = 0;  // concave radius < 0.125"  (tool dia < 0.25")
        int total  = 0;
    };

    enum class CliMode
    {
        Analyze,
        ExportGlb,
        Thumbnail
    };

    struct CliOptions
    {
        CliMode mode = CliMode::Analyze;
        std::string inputPath;
        std::string outputPath;
        int width = 500;
        int height = 500;
    };

    std::string escapeJson(const std::string& s)
    {
        std::ostringstream out;
        for (char c : s)
        {
            switch (c)
            {
                case '"': out << "\\\""; break;
                case '\\': out << "\\\\"; break;
                case '\b': out << "\\b"; break;
                case '\f': out << "\\f"; break;
                case '\n': out << "\\n"; break;
                case '\r': out << "\\r"; break;
                case '\t': out << "\\t"; break;
                default:
                    if (static_cast<unsigned char>(c) < 0x20)
                    {
                        out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                            << static_cast<int>(static_cast<unsigned char>(c))
                            << std::dec << std::setfill(' ');
                    }
                    else
                    {
                        out << c;
                    }
            }
        }
        return out.str();
    }

    std::string toLower(std::string s)
    {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return s;
    }

    std::string trim(const std::string& s)
    {
        std::size_t b = 0;
        while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b])))
            ++b;
        std::size_t e = s.size();
        while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1])))
            --e;
        return s.substr(b, e - b);
    }

    std::string primaryStepLengthUnit(STEPControl_Reader& reader)
    {
        TColStd_SequenceOfAsciiString lengthUnits;
        TColStd_SequenceOfAsciiString angleUnits;
        TColStd_SequenceOfAsciiString solidAngleUnits;
        reader.FileUnits(lengthUnits, angleUnits, solidAngleUnits);

        if (lengthUnits.Length() > 0)
            return trim(lengthUnits.Value(1).ToCString());
        return std::string();
    }

    double unitToInches(const std::string& unit)
    {
        std::string u;
        u.reserve(unit.size());
        for (char c : toLower(unit))
        {
            if (std::isalnum(static_cast<unsigned char>(c)) || c == '_')
                u.push_back(c);
        }

        if (u == "in" || u == "inch" || u == "inches")
            return 1.0;
        if (u == "mm" || u == "millimeter" || u == "millimeters" || u == "millimetre" || u == "millimetres")
            return 1.0 / 25.4;
        if (u == "cm" || u == "centimeter" || u == "centimeters" || u == "centimetre" || u == "centimetres")
            return 1.0 / 2.54;
        if (u == "m" || u == "meter" || u == "meters" || u == "metre" || u == "metres")
            return 39.37007874015748;
        if (u == "ft" || u == "foot" || u == "feet")
            return 12.0;
        if (u == "um" || u == "micrometer" || u == "micrometers" || u == "micrometre" || u == "micrometres")
            return 1.0 / 25400.0;
        if (u == "mil")
            return 0.001;
        if (u == "thou")
            return 1.0;
        return -1.0;
    }

    int countSubShapes(const TopoDS_Shape& shape, TopAbs_ShapeEnum type)
    {
        int count = 0;
        for (TopExp_Explorer exp(shape, type); exp.More(); exp.Next())
            ++count;
        return count;
    }

    void appendCurvatureSamples(const TopoDS_Face& face, double lengthToInches, CurvatureStats& out)
    {
        BRepAdaptor_Surface surf(face, Standard_True);
        Standard_Real uMin = 0.0, uMax = 0.0, vMin = 0.0, vMax = 0.0;
        BRepTools::UVBounds(face, uMin, uMax, vMin, vMax);
        if (!(std::isfinite(uMin) && std::isfinite(uMax) && std::isfinite(vMin) && std::isfinite(vMax)))
            return;
        if (uMax <= uMin || vMax <= vMin)
            return;

        constexpr int kSteps = 5;
        for (int iu = 0; iu < kSteps; ++iu)
        {
            const double u = uMin + (uMax - uMin) * static_cast<double>(iu) / static_cast<double>(kSteps - 1);
            for (int iv = 0; iv < kSteps; ++iv)
            {
                const double v = vMin + (vMax - vMin) * static_cast<double>(iv) / static_cast<double>(kSteps - 1);
                BRepLProp_SLProps props(surf, u, v, 2, Precision::Confusion());
                if (!props.IsCurvatureDefined())
                    continue;

                const double k = std::max(std::abs(props.MinCurvature()), std::abs(props.MaxCurvature()));
                if (!std::isfinite(k))
                    continue;

                out.add(k / lengthToInches);
            }
        }
    }

    // Samples concave curvature across a face's UV domain and buckets each sample
    // by the minimum tool radius that can reach it.
    //
    // Sign convention: BRepLProp_SLProps curvatures are relative to the parametric
    // surface normal (u×v direction), which is independent of the face's topological
    // orientation in the solid.  For a cylinder, k_axial=0 and k_circ=-1/R in the
    // parametric frame regardless of whether it is a hole or a boss.
    //
    // To determine concavity from the solid's perspective we need the curvature
    // relative to the SOLID's outward normal:
    //   FORWARD face  → solid outward normal == parametric normal → use  MaxCurvature()
    //   REVERSED face → solid outward normal == -parametric normal → use -MinCurvature()
    //
    // A cylindrical hole wall is REVERSED: effective max = -MinCurvature() = 1/R > 0 ✓
    // An external cylindrical boss is FORWARD: effective max = MaxCurvature() = 0  ✓
    void sampleToolAccess(const TopoDS_Face& face, double lengthToInches, ToolAccessSamples& out)
    {
        BRepAdaptor_Surface surf(face, Standard_True);
        Standard_Real uMin = 0.0, uMax = 0.0, vMin = 0.0, vMax = 0.0;
        BRepTools::UVBounds(face, uMin, uMax, vMin, vMax);
        if (!(std::isfinite(uMin) && std::isfinite(uMax) && std::isfinite(vMin) && std::isfinite(vMax)))
            return;
        if (uMax <= uMin || vMax <= vMin)
            return;

        const bool isReversed = (face.Orientation() == TopAbs_REVERSED);

        constexpr int kSteps = 5;
        for (int iu = 0; iu < kSteps; ++iu)
        {
            const double u = uMin + (uMax - uMin) * static_cast<double>(iu) / static_cast<double>(kSteps - 1);
            for (int iv = 0; iv < kSteps; ++iv)
            {
                const double v = vMin + (vMax - vMin) * static_cast<double>(iv) / static_cast<double>(kSteps - 1);
                ++out.total;

                BRepLProp_SLProps props(surf, u, v, 2, Precision::Confusion());
                if (!props.IsCurvatureDefined())
                {
                    ++out.large;  // treat undefined as open/accessible
                    continue;
                }

                // Effective max concave curvature from the solid's outward-normal frame.
                // Positive value means the surface curves toward the accessible void side
                // (a tool must fit into the concavity).
                const double effectiveMaxK = isReversed
                    ? -props.MinCurvature()
                    :  props.MaxCurvature();

                if (!std::isfinite(effectiveMaxK) || effectiveMaxK <= 0.0)
                {
                    ++out.large;  // convex or flat — no tool-size constraint
                    continue;
                }

                // Concave feature: radius = 1/curvature, converted to inches
                const double radiusIn = (1.0 / effectiveMaxK) * lengthToInches;
                if      (radiusIn > 0.25)   ++out.large;
                else if (radiusIn >= 0.125)  ++out.medium;
                else                         ++out.small;
            }
        }
    }

    void printUsage()
    {
        std::cerr << "Usage:\n"
                  << "  StepMetricsCli <input.step|input.stp> [output.json]\n"
                  << "  StepMetricsCli --analyze <input.step|input.stp> [output.json]\n"
                  << "  StepMetricsCli --export-glb <input.step|input.stp> <output.glb>\n"
                  << "  StepMetricsCli --thumbnail <input.step|input.stp> <output.png> <width> <height>\n";
    }

    bool parseArgs(int argc, char* argv[], CliOptions& out)
    {
        if (argc >= 2 && std::string(argv[1]) == "--export-glb")
        {
            if (argc != 4)
                return false;
            out.mode = CliMode::ExportGlb;
            out.inputPath = argv[2];
            out.outputPath = argv[3];
            return true;
        }

        if (argc >= 2 && std::string(argv[1]) == "--thumbnail")
        {
            if (argc != 6)
                return false;
            out.mode = CliMode::Thumbnail;
            out.inputPath = argv[2];
            out.outputPath = argv[3];
            out.width = std::max(64, std::atoi(argv[4]));
            out.height = std::max(64, std::atoi(argv[5]));
            return true;
        }

        if (argc >= 2 && std::string(argv[1]) == "--analyze")
        {
            if (argc < 3 || argc > 4)
                return false;
            out.mode = CliMode::Analyze;
            out.inputPath = argv[2];
            out.outputPath = (argc == 4) ? argv[3] : std::string();
            return true;
        }

        if (argc < 2 || argc > 3)
            return false;

        out.mode = CliMode::Analyze;
        out.inputPath = argv[1];
        out.outputPath = (argc == 3) ? argv[2] : std::string();
        return true;
    }

    double computeLinearDeflection(const TopoDS_Shape& shape)
    {
        Bnd_Box bbox;
        BRepBndLib::Add(shape, bbox);
        if (bbox.IsVoid())
            return 0.1;

        Standard_Real xMin = 0.0, yMin = 0.0, zMin = 0.0, xMax = 0.0, yMax = 0.0, zMax = 0.0;
        bbox.Get(xMin, yMin, zMin, xMax, yMax, zMax);
        const double dx = xMax - xMin;
        const double dy = yMax - yMin;
        const double dz = zMax - zMin;
        const double diag = std::sqrt(dx * dx + dy * dy + dz * dz);
        return std::max(0.01, diag * 0.001);
    }

    void meshForExport(const TopoDS_Shape& shape)
    {
        if (shape.IsNull())
            return;

        const double linearDeflection = computeLinearDeflection(shape);
        constexpr double angularDeflection = 0.35;
        BRepMesh_IncrementalMesh mesher(shape, linearDeflection, Standard_False, angularDeflection, Standard_True);
        mesher.Perform();
    }

    bool loadStepDocument(const std::string& inputPath,
                          Handle(TDocStd_Document)& doc,
                          TDF_LabelSequence& freeShapes)
    {
        Handle(XCAFApp_Application) app = XCAFApp_Application::GetApplication();
        app->NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc);
        if (doc.IsNull())
        {
            std::cerr << "Failed to create XCAF document.\n";
            return false;
        }

        STEPCAFControl_Reader reader;
        reader.SetColorMode(Standard_True);
        reader.SetNameMode(Standard_True);
        reader.SetLayerMode(Standard_True);
        reader.SetPropsMode(Standard_True);
        reader.SetMatMode(Standard_True);

        if (!reader.Perform(inputPath.c_str(), doc, Message_ProgressRange()))
        {
            std::cerr << "Failed to read STEP file: " << inputPath << "\n";
            return false;
        }

        Handle(XCAFDoc_ShapeTool) shapeTool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
        if (shapeTool.IsNull())
        {
            std::cerr << "Failed to access XCAF shape tool.\n";
            return false;
        }

        shapeTool->GetFreeShapes(freeShapes);
        if (freeShapes.Length() == 0)
        {
            std::cerr << "No shapes found in STEP document.\n";
            return false;
        }

        for (Standard_Integer i = 1; i <= freeShapes.Length(); ++i)
        {
            meshForExport(shapeTool->GetShape(freeShapes.Value(i)));
        }

        return true;
    }

    int analyzeStep(const CliOptions& options)
    {
        STEPControl_Reader reader;
        const IFSelect_ReturnStatus status = reader.ReadFile(options.inputPath.c_str());
        if (status != IFSelect_RetDone)
        {
            std::cerr << "Failed to read STEP file: " << options.inputPath << "\n";
            return 2;
        }

        if (!reader.TransferRoots())
        {
            std::cerr << "Failed to transfer STEP roots.\n";
            return 3;
        }

        TopoDS_Shape shape = reader.OneShape();
        if (shape.IsNull())
        {
            std::cerr << "STEP transfer produced empty shape.\n";
            return 4;
        }

        std::string sourceStepUnit = primaryStepLengthUnit(reader);
        if (sourceStepUnit.empty())
        {
            if (const char* unitRaw = Interface_Static::CVal("xstep.cascade.unit"))
                sourceStepUnit = unitRaw;
        }
        if (sourceStepUnit.empty())
            sourceStepUnit = "mm";

        double lengthToInches = unitToInches(sourceStepUnit);
        if (lengthToInches <= 0.0)
            lengthToInches = 1.0 / 25.4;

        const int solids = countSubShapes(shape, TopAbs_SOLID);
        const int faces = countSubShapes(shape, TopAbs_FACE);
        const int edges = countSubShapes(shape, TopAbs_EDGE);
        const int vertices = countSubShapes(shape, TopAbs_VERTEX);

        int planarCount = 0;
        int cylCount = 0;
        int conCount = 0;
        int otherCount = 0;
        double totalAreaNative = 0.0;
        CurvatureStats cylCurv;
        CurvatureStats conCurv;
        CurvatureStats combinedCurv;

        // Tool-accessibility accumulators (native area units — ratios cancel units)
        double largeToolArea  = 0.0;  // accessible by tools > 0.5" dia
        double mediumToolArea = 0.0;  // needs 0.25–0.5" dia tool
        double smallToolArea  = 0.0;  // needs < 0.25" dia tool

        // Cylinder axes collected for rotational symmetry analysis
        std::vector<CylFaceAxisInfo> cylFaceAxes;

        for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next())
        {
            const TopoDS_Face face = TopoDS::Face(exp.Current());

            GProp_GProps faceProps;
            BRepGProp::SurfaceProperties(face, faceProps);
            const double faceArea = std::abs(faceProps.Mass());
            totalAreaNative += faceArea;

            BRepAdaptor_Surface surf(face, Standard_True);
            const GeomAbs_SurfaceType surfType = surf.GetType();

            switch (surfType)
            {
                case GeomAbs_Plane:    ++planarCount; break;
                case GeomAbs_Cylinder:
                    ++cylCount;
                    appendCurvatureSamples(face, lengthToInches, cylCurv);
                    cylFaceAxes.push_back({surf.Cylinder().Axis(), faceArea});
                    break;
                case GeomAbs_Cone:     ++conCount;    appendCurvatureSamples(face, lengthToInches, conCurv); break;
                default:               ++otherCount;  break;
            }

            // Tool accessibility: planar faces are always reachable by large tools;
            // all curved faces are sampled for concave curvature and area-distributed.
            if (surfType == GeomAbs_Plane)
            {
                largeToolArea += faceArea;
            }
            else
            {
                ToolAccessSamples s;
                sampleToolAccess(face, lengthToInches, s);
                if (s.total > 0)
                {
                    largeToolArea  += faceArea * static_cast<double>(s.large)  / s.total;
                    mediumToolArea += faceArea * static_cast<double>(s.medium) / s.total;
                    smallToolArea  += faceArea * static_cast<double>(s.small)  / s.total;
                }
                else
                {
                    largeToolArea += faceArea;
                }
            }
        }

        if (cylCurv.sampleCount)
        {
            combinedCurv.sampleCount += cylCurv.sampleCount;
            combinedCurv.sum += cylCurv.sum;
            combinedCurv.minValue = std::min(combinedCurv.minValue, cylCurv.minValue);
            combinedCurv.maxValue = std::max(combinedCurv.maxValue, cylCurv.maxValue);
        }
        if (conCurv.sampleCount)
        {
            combinedCurv.sampleCount += conCurv.sampleCount;
            combinedCurv.sum += conCurv.sum;
            combinedCurv.minValue = std::min(combinedCurv.minValue, conCurv.minValue);
            combinedCurv.maxValue = std::max(combinedCurv.maxValue, conCurv.maxValue);
        }

        GProp_GProps volProps;
        BRepGProp::VolumeProperties(shape, volProps);
        const double volumeNative = std::abs(volProps.Mass());

        Bnd_Box bbox;
        BRepBndLib::Add(shape, bbox);
        Standard_Real xMin = 0.0, yMin = 0.0, zMin = 0.0, xMax = 0.0, yMax = 0.0, zMax = 0.0;
        const bool hasBbox = !bbox.IsVoid();
        if (hasBbox)
            bbox.Get(xMin, yMin, zMin, xMax, yMax, zMax);

        const double areaIn2 = totalAreaNative * lengthToInches * lengthToInches;
        const double volumeIn3 = volumeNative * lengthToInches * lengthToInches * lengthToInches;

        const double classifiedArea = largeToolArea + mediumToolArea + smallToolArea;
        const double pctLarge  = classifiedArea > 0.0 ? 100.0 * largeToolArea  / classifiedArea : 0.0;
        const double pctMedium = classifiedArea > 0.0 ? 100.0 * mediumToolArea / classifiedArea : 0.0;
        const double pctSmall  = classifiedArea > 0.0 ? 100.0 * smallToolArea  / classifiedArea : 0.0;

        // ── Rotational symmetry analysis ─────────────────────────────────────────────

        // Total cylindrical area and its share of the whole surface
        double totalCylAreaNative = 0.0;
        for (const CylFaceAxisInfo& cfi : cylFaceAxes)
            totalCylAreaNative += cfi.area;
        const double cylAreaPercent = (totalAreaNative > 0.0)
            ? 100.0 * totalCylAreaNative / totalAreaNative : 0.0;

        // Group cylinder axes by direction.
        // Anti-parallel axes (same geometric axis, opposite sense) are canonicalised
        // so the component with the largest absolute value is always positive.
        struct AxisGroup
        {
            gp_Dir dir;
            gp_XYZ locWeightedSum = gp_XYZ(0.0, 0.0, 0.0);
            double areaSum        = 0.0;
            int    count          = 0;
        };

        constexpr double kAxisAngleTolRad = 0.034907; // 2 degrees

        auto canonDir = [](const gp_Dir& d) -> gp_Dir
        {
            const double ax = std::abs(d.X()), ay = std::abs(d.Y()), az = std::abs(d.Z());
            double sign;
            if (ax >= ay && ax >= az) sign = (d.X() >= 0.0) ? 1.0 : -1.0;
            else if (ay >= az)         sign = (d.Y() >= 0.0) ? 1.0 : -1.0;
            else                       sign = (d.Z() >= 0.0) ? 1.0 : -1.0;
            return sign >= 0.0 ? d : d.Reversed();
        };

        std::vector<AxisGroup> axisGroups;
        for (const CylFaceAxisInfo& cfi : cylFaceAxes)
        {
            const gp_Dir cd = canonDir(cfi.axis.Direction());
            bool found = false;
            for (AxisGroup& grp : axisGroups)
            {
                if (grp.dir.Angle(cd) < kAxisAngleTolRad)
                {
                    grp.locWeightedSum.Add(cfi.axis.Location().XYZ().Multiplied(cfi.area));
                    grp.areaSum += cfi.area;
                    ++grp.count;
                    found = true;
                    break;
                }
            }
            if (!found)
            {
                AxisGroup g;
                g.dir            = cd;
                g.locWeightedSum = cfi.axis.Location().XYZ().Multiplied(cfi.area);
                g.areaSum        = cfi.area;
                g.count          = 1;
                axisGroups.push_back(g);
            }
        }

        // Find dominant group (most cylindrical area)
        int dominantIdx = -1;
        for (int i = 0; i < static_cast<int>(axisGroups.size()); ++i)
            if (dominantIdx < 0 || axisGroups[i].areaSum > axisGroups[dominantIdx].areaSum)
                dominantIdx = i;

        // Output values — sensible defaults for the no-cylinder case
        double      symConfidence    = 0.0;
        bool        hasStrongSym     = false;
        gp_Dir      mainAxisDir(0.0, 0.0, 1.0);
        gp_Pnt      mainAxisPt(0.0, 0.0, 0.0);
        std::string dominantProcess  = "Milled";

        if (dominantIdx >= 0 && totalCylAreaNative > 0.0)
        {
            const AxisGroup& dom = axisGroups[dominantIdx];
            mainAxisDir = dom.dir;

            // Area-weighted centroid of the axes in this group
            const gp_Pnt avgLoc(dom.locWeightedSum.Divided(dom.areaSum));

            // Project center of mass onto dominant axis → clean, on-axis reference point
            const gp_Pnt com = volProps.CentreOfMass();
            const gp_Vec comVec(avgLoc, com);
            const double t   = comVec.Dot(gp_Vec(mainAxisDir));
            mainAxisPt       = avgLoc.Translated(gp_Vec(mainAxisDir).Multiplied(t));

            // Bounding-sphere radius for normalising the CoM-to-axis distance
            double bsR = 1.0;
            if (hasBbox)
            {
                const double ddx = xMax-xMin, ddy = yMax-yMin, ddz = zMax-zMin;
                bsR = std::sqrt(ddx*ddx + ddy*ddy + ddz*ddz) / 2.0;
                if (bsR < 1e-10) bsR = 1.0;
            }

            // Distance from CoM to dominant axis line
            const gp_Vec perp     = comVec - gp_Vec(mainAxisDir).Multiplied(t);
            const double distCoM  = perp.Magnitude();
            const double normDist = distCoM / bsR;

            // axisCoverage: what fraction of all cyl area sits on the dominant axis
            const double axisCoverage = dom.areaSum / totalCylAreaNative;

            // proximityFactor: penalise axes that don't pass near the CoM
            // (a turned part's axis runs through or very near its CoM)
            const double proximityFactor = 1.0 / (1.0 + normDist * 4.0);

            symConfidence = std::min(1.0, axisCoverage * (0.55 + 0.45 * proximityFactor));

            hasStrongSym = (cylAreaPercent     >= 30.0)
                        && (axisCoverage       >= 0.65)
                        && (symConfidence      >= 0.45);

            if      (cylAreaPercent >= 55.0 && symConfidence >= 0.65) dominantProcess = "Turned";
            else if (cylAreaPercent <  15.0)                           dominantProcess = "Milled";
            else                                                        dominantProcess = "Hybrid";
        }

        const auto percent = [faces](int count) -> double {
            return faces > 0 ? 100.0 * static_cast<double>(count) / static_cast<double>(faces) : 0.0;
        };

        std::ostringstream json;
        json << std::fixed << std::setprecision(6);
        json << "{\n";
        json << "  \"input_file\": \"" << escapeJson(options.inputPath) << "\",\n";
        json << "  \"units\": {\n";
        json << "    \"source_step_length_unit\": \"" << escapeJson(sourceStepUnit) << "\"\n";
        json << "  },\n";
        json << "  \"topology_counts\": {\n";
        json << "    \"solids\": " << solids << ",\n";
        json << "    \"faces\": " << faces << ",\n";
        json << "    \"edges\": " << edges << ",\n";
        json << "    \"vertices\": " << vertices << "\n";
        json << "  },\n";
        json << "  \"geometry\": {\n";
        json << "    \"total_surface_area\": {\"value\": " << areaIn2 << ", \"unit\": \"in^2\"},\n";
        json << "    \"part_volume\": {\"value\": " << volumeIn3 << ", \"unit\": \"in^3\"},\n";
        json << "    \"bounding_box\": {\n";
        if (hasBbox)
        {
            const double x0 = xMin * lengthToInches;
            const double y0 = yMin * lengthToInches;
            const double z0 = zMin * lengthToInches;
            const double x1 = xMax * lengthToInches;
            const double y1 = yMax * lengthToInches;
            const double z1 = zMax * lengthToInches;
            const double dx = (x1 - x0);
            const double dy = (y1 - y0);
            const double dz = (z1 - z0);
            json << "      \"min\": {\"value\": [" << x0 << ", " << y0 << ", " << z0 << "], \"unit\": \"in\"},\n";
            json << "      \"max\": {\"value\": [" << x1 << ", " << y1 << ", " << z1 << "], \"unit\": \"in\"},\n";
            json << "      \"dimensions_lwh\": {\"value\": [" << dx << ", " << dy << ", " << dz << "], \"unit\": \"in\"},\n";
            json << "      \"volume\": {\"value\": " << (dx * dy * dz) << ", \"unit\": \"in^3\"}\n";
        }
        else
        {
            json << "      \"min\": null,\n";
            json << "      \"max\": null,\n";
            json << "      \"dimensions_lwh\": null,\n";
            json << "      \"volume\": null\n";
        }
        json << "    },\n";
        json << "    \"tool_accessibility\": {\n";
        json << "      \"method\": \"surface-area weighted by concave curvature radius sampled on 5x5 UV grid per face\",\n";
        json << "      \"note\": \"percentages reflect share of total surface area, not removed-material volume\",\n";
        json << "      \"large_tool_gt_0_5in_dia\": {\"percent\": " << pctLarge  << "},\n";
        json << "      \"medium_tool_0_25_to_0_5in_dia\": {\"percent\": " << pctMedium << "},\n";
        json << "      \"small_tool_lt_0_25in_dia\": {\"percent\": " << pctSmall  << "}\n";
        json << "    },\n";
        json << "    \"rotational_symmetry\": {\n";
        json << "      \"cylindrical_area_percent\": " << cylAreaPercent << ",\n";
        json << "      \"has_strong_rotational_symmetry\": " << (hasStrongSym ? "true" : "false") << ",\n";
        json << "      \"symmetry_confidence\": " << symConfidence << ",\n";
        json << "      \"main_axis_direction\": ["
             << mainAxisDir.X() << ", " << mainAxisDir.Y() << ", " << mainAxisDir.Z() << "],\n";
        json << "      \"main_axis_point\": ["
             << mainAxisPt.X() * lengthToInches << ", "
             << mainAxisPt.Y() * lengthToInches << ", "
             << mainAxisPt.Z() * lengthToInches << "],\n";
        json << "      \"dominant_process\": \"" << dominantProcess << "\"\n";
        json << "    }\n";
        json << "  },\n";
        json << "  \"face_type_distribution\": {\n";
        json << "    \"planar\": {\"count\": " << planarCount << ", \"percent\": " << percent(planarCount) << "},\n";
        json << "    \"cylindrical\": {\"count\": " << cylCount << ", \"percent\": " << percent(cylCount) << "},\n";
        json << "    \"conical\": {\"count\": " << conCount << ", \"percent\": " << percent(conCount) << "},\n";
        json << "    \"other\": {\"count\": " << otherCount << ", \"percent\": " << percent(otherCount) << "}\n";
        json << "  },\n";
        json << "  \"curvature_statistics_per_in\": {\n";
        json << "    \"method\": \"max absolute principal curvature sampled on UV grid for cylindrical/conical faces\",\n";
        json << "    \"unit\": \"1/in\",\n";
        json << "    \"cylindrical\": ";
        if (!cylCurv.sampleCount)
        {
            json << "{\"samples\": 0, \"min\": null, \"avg\": null, \"max\": null}";
        }
        else
        {
            json << "{\"samples\": " << cylCurv.sampleCount << ", "
                 << "\"min\": {\"value\": " << cylCurv.minValue << ", \"unit\": \"1/in\"}, "
                 << "\"avg\": {\"value\": " << cylCurv.avg() << ", \"unit\": \"1/in\"}, "
                 << "\"max\": {\"value\": " << cylCurv.maxValue << ", \"unit\": \"1/in\"}}";
        }
        json << ",\n";
        json << "    \"conical\": ";
        if (!conCurv.sampleCount)
        {
            json << "{\"samples\": 0, \"min\": null, \"avg\": null, \"max\": null}";
        }
        else
        {
            json << "{\"samples\": " << conCurv.sampleCount << ", "
                 << "\"min\": {\"value\": " << conCurv.minValue << ", \"unit\": \"1/in\"}, "
                 << "\"avg\": {\"value\": " << conCurv.avg() << ", \"unit\": \"1/in\"}, "
                 << "\"max\": {\"value\": " << conCurv.maxValue << ", \"unit\": \"1/in\"}}";
        }
        json << ",\n";
        json << "    \"combined\": ";
        if (!combinedCurv.sampleCount)
        {
            json << "{\"samples\": 0, \"min\": null, \"avg\": null, \"max\": null}";
        }
        else
        {
            json << "{\"samples\": " << combinedCurv.sampleCount << ", "
                 << "\"min\": {\"value\": " << combinedCurv.minValue << ", \"unit\": \"1/in\"}, "
                 << "\"avg\": {\"value\": " << combinedCurv.avg() << ", \"unit\": \"1/in\"}, "
                 << "\"max\": {\"value\": " << combinedCurv.maxValue << ", \"unit\": \"1/in\"}}";
        }
        json << "\n";
        json << "  }\n";
        json << "}\n";

        if (!options.outputPath.empty())
        {
            std::ofstream out(options.outputPath, std::ios::binary);
            if (!out)
            {
                std::cerr << "Failed to open output file: " << options.outputPath << "\n";
                return 5;
            }
            out << json.str();
        }
        else
        {
            std::cout << json.str();
        }

        return 0;
    }

    int exportGlb(const CliOptions& options)
    {
        Handle(TDocStd_Document) doc;
        TDF_LabelSequence freeShapes;
        if (!loadStepDocument(options.inputPath, doc, freeShapes))
            return 21;

        RWGltf_CafWriter writer(TCollection_AsciiString(options.outputPath.c_str()), Standard_True);
        writer.SetTransformationFormat(RWGltf_WriterTrsfFormat_Compact);
#if OCC_VERSION_HEX >= 0x070700
        writer.SetMergeFaces(Standard_True);
        writer.SetSplitIndices16(Standard_True);
        writer.SetToEmbedTexturesInGlb(Standard_True);
#endif

        TColStd_IndexedDataMapOfStringString fileInfo;
        fileInfo.Add("generator", "StepMetricsCli");
        fileInfo.Add("source", options.inputPath.c_str());

        if (!writer.Perform(doc, fileInfo, Message_ProgressRange()))
        {
            std::cerr << "Failed to write GLB output: " << options.outputPath << "\n";
            return 24;
        }

        return 0;
    }

    int generateThumbnail(const CliOptions& options)
    {
#ifdef _WIN32
        Handle(TDocStd_Document) doc;
        TDF_LabelSequence freeShapes;
        if (!loadStepDocument(options.inputPath, doc, freeShapes))
            return 30;

        Handle(Aspect_DisplayConnection) display = new Aspect_DisplayConnection();
        Handle(OpenGl_GraphicDriver) driver = new OpenGl_GraphicDriver(display);
        Handle(V3d_Viewer) viewer = new V3d_Viewer(driver);
        viewer->SetDefaultBackgroundColor(Quantity_Color(Quantity_NOC_WHITE));
        viewer->SetDefaultShadingModel(Graphic3d_TypeOfShadingModel_Phong);
        viewer->SetDefaultLights();
        viewer->SetLightOn();

        Handle(AIS_InteractiveContext) context = new AIS_InteractiveContext(viewer);
        Handle(V3d_View) view = viewer->CreateView();
        view->SetBackgroundColor(Quantity_Color(Quantity_NOC_WHITE));
        view->SetImmediateUpdate(Standard_False);
        view->SetProj(V3d_XposYnegZpos);
        view->ChangeRenderingParams().ToShowStats = Standard_False;

        Handle(WNT_WClass) windowClass = new WNT_WClass(
            TCollection_AsciiString("StepMetricsThumbWindow"),
            reinterpret_cast<Standard_Address>(DefWindowProcW),
            CS_OWNDC,
            0,
            0,
            NULL,
            NULL,
            TCollection_AsciiString());
        Handle(WNT_Window) window = new WNT_Window(
            "StepMetricsThumbnail",
            windowClass,
            WS_POPUP,
            0,
            0,
            options.width,
            options.height,
            Quantity_NOC_WHITE,
            0,
            0,
            0);
        window->Map(SW_HIDE);
        view->SetWindow(window);
        view->MustBeResized();
        Handle(XCAFDoc_ShapeTool) shapeTool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
        for (Standard_Integer i = 1; i <= freeShapes.Length(); ++i)
        {
            const TDF_Label& shapeLabel = freeShapes.Value(i);
            Handle(XCAFPrs_AISObject) prs = new XCAFPrs_AISObject(shapeLabel);
            prs->DispatchStyles(Standard_True);
            context->Display(prs, AIS_Shaded, -1, Standard_False);

            const TopoDS_Shape shape = shapeTool->GetShape(shapeLabel);
            if (!shape.IsNull())
            {
                Handle(AIS_Shape) wirePrs = new AIS_Shape(shape);
                context->SetColor(wirePrs, Quantity_Color(Quantity_NOC_BLACK), Standard_False);
                context->SetWidth(wirePrs, 1.5, Standard_False);
                context->Display(wirePrs, AIS_WireFrame, -1, Standard_False);
            }
        }

        context->UpdateCurrentViewer();
        view->FitAll(0.01, Standard_False);
        view->ZFitAll();
        view->Redraw();

        Image_AlienPixMap image;
        if (!view->ToPixMap(image, options.width, options.height))
        {
            std::cerr << "Failed to render thumbnail pixmap.\n";
            return 31;
        }

        if (!image.Save(TCollection_AsciiString(options.outputPath.c_str())))
        {
            std::cerr << "Failed to save thumbnail image: " << options.outputPath << "\n";
            return 32;
        }

        view->Remove();
        return 0;
#else
        std::cout << "{\"error\":\"thumbnail rendering is not supported on this platform\"}\n";
        return 1;
#endif
    }
}

int main(int argc, char* argv[])
{
    CliOptions options;
    if (!parseArgs(argc, argv, options))
    {
        printUsage();
        return 1;
    }

    try
    {
        switch (options.mode)
        {
            case CliMode::Analyze:
                return analyzeStep(options);
            case CliMode::ExportGlb:
                return exportGlb(options);
            case CliMode::Thumbnail:
                return generateThumbnail(options);
        }
        return 1;
    }
    catch (const Standard_Failure& e)
    {
        std::cerr << "OpenCASCADE error: " << e.GetMessageString() << "\n";
        return 10;
    }
    catch (const std::exception& e)
    {
        std::cerr << "Error: " << e.what() << "\n";
        return 11;
    }
}



