#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  define NOMINMAX
#  include <windows.h>
#endif

#include <algorithm>
#include <chrono>
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
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Tool.hxx>
#include <BRepTools.hxx>
#include <BRepTopAdaptor_FClass2d.hxx>
#include <IntCurvesFace_ShapeIntersector.hxx>
#include <gp_Lin.hxx>
#include <gp_Pnt2d.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <Geom_CylindricalSurface.hxx>
#include <Geom_OffsetSurface.hxx>
#include <Geom_RectangularTrimmedSurface.hxx>
#include <Geom_Surface.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopLoc_Location.hxx>
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
#include <TopAbs_State.hxx>
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

    // Ubuntu OCCT headers only forward-declare gp_Cylinder, so adaptor.Cylinder()
    // cannot be used there. Pull the axis from the underlying Geom surface instead.
    bool cylindricalFaceAxis(const TopoDS_Face& face, gp_Ax1& axis)
    {
        TopLoc_Location loc;
        Handle(Geom_Surface) surface = BRep_Tool::Surface(face, loc);
        while (!surface.IsNull())
        {
            Handle(Geom_CylindricalSurface) cylinder =
                Handle(Geom_CylindricalSurface)::DownCast(surface);
            if (!cylinder.IsNull())
            {
                axis = cylinder->Axis();
                if (!loc.IsIdentity())
                    axis.Transform(loc.Transformation());
                return true;
            }

            Handle(Geom_RectangularTrimmedSurface) trimmed =
                Handle(Geom_RectangularTrimmedSurface)::DownCast(surface);
            if (!trimmed.IsNull())
            {
                surface = trimmed->BasisSurface();
                continue;
            }

            Handle(Geom_OffsetSurface) offset =
                Handle(Geom_OffsetSurface)::DownCast(surface);
            if (!offset.IsNull())
            {
                surface = offset->BasisSurface();
                continue;
            }

            return false;
        }
        return false;
    }

    // Radius of a cylindrical face's underlying surface (same unwrapping as
    // cylindricalFaceAxis). Offset surfaces are skipped: their radius differs from the basis.
    bool cylindricalFaceRadius(const TopoDS_Face& face, double& radius)
    {
        TopLoc_Location loc;
        Handle(Geom_Surface) surface = BRep_Tool::Surface(face, loc);
        while (!surface.IsNull())
        {
            Handle(Geom_CylindricalSurface) cylinder =
                Handle(Geom_CylindricalSurface)::DownCast(surface);
            if (!cylinder.IsNull())
            {
                radius = cylinder->Radius();
                return true;
            }

            Handle(Geom_RectangularTrimmedSurface) trimmed =
                Handle(Geom_RectangularTrimmedSurface)::DownCast(surface);
            if (!trimmed.IsNull())
            {
                surface = trimmed->BasisSurface();
                continue;
            }

            return false;
        }
        return false;
    }

    // One cylindrical face, reduced to what hole detection needs. Axial positions are
    // measured along the face's own axis line from its location point.
    struct CylPiece
    {
        gp_Pnt axisLoc;
        gp_Dir axisDir;      // canonicalised (largest component positive)
        double radius  = 0.0;
        double t0      = 0.0; // axial start along axisDir from axisLoc
        double t1      = 0.0; // axial end
        double arcRad  = 0.0; // angular span of the face
        bool   concave = false;
    };

    gp_Dir canonicalDir(const gp_Dir& d)
    {
        const double ax = std::abs(d.X()), ay = std::abs(d.Y()), az = std::abs(d.Z());
        double sign;
        if (ax >= ay && ax >= az) sign = (d.X() >= 0.0) ? 1.0 : -1.0;
        else if (ay >= az)         sign = (d.Y() >= 0.0) ? 1.0 : -1.0;
        else                       sign = (d.Z() >= 0.0) ? 1.0 : -1.0;
        return sign >= 0.0 ? d : d.Reversed();
    }

    // Distance between two (parallel) axis lines, both given by point + direction.
    double axisLineDistance(const gp_Pnt& p1, const gp_Dir& d1, const gp_Pnt& p2)
    {
        const gp_Vec v(p1, p2);
        return v.Crossed(gp_Vec(d1)).Magnitude();
    }

    // Builds a CylPiece from a cylindrical face. Concavity comes from comparing the
    // face's material-side normal (parametric normal, flipped for REVERSED faces) with the
    // radial direction away from the axis: pointing toward the axis = a hole wall.
    bool makeCylPiece(const TopoDS_Face& face, CylPiece& out)
    {
        gp_Ax1 axis;
        double radius = 0.0;
        if (!cylindricalFaceAxis(face, axis) || !cylindricalFaceRadius(face, radius))
            return false;
        if (!(radius > 0.0))
            return false;

        Standard_Real uMin = 0.0, uMax = 0.0, vMin = 0.0, vMax = 0.0;
        BRepTools::UVBounds(face, uMin, uMax, vMin, vMax);
        if (!(std::isfinite(uMin) && std::isfinite(uMax) && std::isfinite(vMin) && std::isfinite(vMax)))
            return false;
        if (uMax <= uMin || vMax <= vMin)
            return false;

        BRepAdaptor_Surface surf(face, Standard_True);
        const double uMid = 0.5 * (uMin + uMax);
        const double vMid = 0.5 * (vMin + vMax);

        const gp_Dir dir = canonicalDir(axis.Direction());
        const gp_Pnt loc = axis.Location();

        const gp_Pnt pA = surf.Value(uMid, vMin);
        const gp_Pnt pB = surf.Value(uMid, vMax);
        const double tA = gp_Vec(loc, pA).Dot(gp_Vec(dir));
        const double tB = gp_Vec(loc, pB).Dot(gp_Vec(dir));

        BRepLProp_SLProps props(surf, uMid, vMid, 1, Precision::Confusion());
        if (!props.IsNormalDefined())
            return false;
        gp_Dir normal = props.Normal();
        if (face.Orientation() == TopAbs_REVERSED)
            normal.Reverse();

        const gp_Pnt pMid = surf.Value(uMid, vMid);
        const double tMid = gp_Vec(loc, pMid).Dot(gp_Vec(dir));
        const gp_Pnt foot = loc.Translated(gp_Vec(dir).Multiplied(tMid));
        const gp_Vec radial(foot, pMid);
        if (radial.Magnitude() < Precision::Confusion())
            return false;

        out.axisLoc = loc;
        out.axisDir = dir;
        out.radius  = radius;
        out.t0      = std::min(tA, tB);
        out.t1      = std::max(tA, tB);
        out.arcRad  = std::min(uMax - uMin, 2.0 * M_PI);
        out.concave = gp_Vec(normal).Dot(radial) < 0.0;
        return true;
    }

    struct DetectedHole
    {
        gp_Pnt axisLoc;      // reference point of the hole's axis line
        gp_Dir axisDir;
        double radius   = 0.0;
        double t0       = 0.0;
        double t1       = 0.0;
        double arcRad   = 0.0;
        int    faces    = 0;
        int    through  = -1; // 1 through, 0 blind, -1 unknown
        int    stackId  = 0;
        bool   largestInStack = false;
        int    stackSize = 1;
    };

    // Air (OUT) at a point? Used to tell through holes from blind ones. One classifier is
    // built per part and reused -- constructing it per query is expensive on hole-heavy parts.
    int classifyOut(BRepClass3d_SolidClassifier& classifier, const gp_Pnt& p, double tol)
    {
        try
        {
            classifier.Perform(p, tol);
            const TopAbs_State st = classifier.State();
            if (st == TopAbs_OUT) return 1;
            if (st == TopAbs_IN)  return 0;
            return -1;
        }
        catch (...)
        {
            return -1;
        }
    }

    // Groups concave cylindrical pieces into holes: same radius, parallel and coaxial axes,
    // overlapping axial extent. B-rep kernels often split one hole into two 180 deg faces
    // (or more, around cross-holes), so a group becomes a hole when its pieces together
    // cover at least 300 deg of arc -- the same rule the WeWeb step-viewer applies to meshes,
    // here on exact geometry. Depth = axial extent of the cylindrical wall (drill point
    // excluded). Through = the space just past both ends lies outside the solid.
    std::vector<DetectedHole> detectHoles(const TopoDS_Shape& shape, const std::vector<CylPiece>& pieces)
    {
        std::vector<DetectedHole> holes;
        std::vector<bool> used(pieces.size(), false);
        BRepClass3d_SolidClassifier classifier(shape);
        constexpr double kParallelTolRad = 0.00873; // 0.5 deg
        constexpr double kMinHoleArcRad  = 5.23599; // 300 deg

        for (std::size_t i = 0; i < pieces.size(); ++i)
        {
            if (used[i] || !pieces[i].concave)
                continue;
            const CylPiece& base = pieces[i];
            const double rTol = std::max(1e-4, base.radius * 0.005);
            const double axTol = std::max(1e-3, base.radius * 0.01);

            // Members measured in the base piece's axial frame.
            DetectedHole h;
            h.axisLoc = base.axisLoc;
            h.axisDir = base.axisDir;
            h.radius  = base.radius;
            h.t0      = base.t0;
            h.t1      = base.t1;
            h.arcRad  = 0.0;
            h.faces   = 0;

            std::vector<std::size_t> members;
            for (std::size_t j = i; j < pieces.size(); ++j)
            {
                if (used[j] || !pieces[j].concave)
                    continue;
                const CylPiece& c = pieces[j];
                if (std::abs(c.radius - base.radius) > rTol)
                    continue;
                if (base.axisDir.Angle(c.axisDir) > kParallelTolRad)
                    continue;
                if (axisLineDistance(base.axisLoc, base.axisDir, c.axisLoc) > axTol)
                    continue;
                members.push_back(j);
            }

            // Re-express member extents in the base frame and merge overlapping bands.
            struct Band { double t0; double t1; double arc; int faces; std::vector<std::size_t> idx; };
            std::vector<Band> bands;
            for (std::size_t j : members)
            {
                const CylPiece& c = pieces[j];
                const double shift = gp_Vec(base.axisLoc, c.axisLoc).Dot(gp_Vec(base.axisDir));
                const double c0 = c.t0 + shift;
                const double c1 = c.t1 + shift;
                bool merged = false;
                for (Band& b : bands)
                {
                    if (c0 <= b.t1 + axTol && c1 >= b.t0 - axTol)
                    {
                        b.t0 = std::min(b.t0, c0);
                        b.t1 = std::max(b.t1, c1);
                        b.arc += c.arcRad;
                        ++b.faces;
                        b.idx.push_back(j);
                        merged = true;
                        break;
                    }
                }
                if (!merged)
                    bands.push_back(Band{c0, c1, c.arcRad, 1, std::vector<std::size_t>{j}});
            }

            for (const Band& b : bands)
            {
                if (b.arc < kMinHoleArcRad)
                    continue;
                DetectedHole hole = h;
                hole.t0 = b.t0;
                hole.t1 = b.t1;
                hole.arcRad = std::min(b.arc, 2.0 * M_PI);
                hole.faces = b.faces;
                for (std::size_t j : b.idx)
                    used[j] = true;

                const double eps = std::max(0.05, hole.radius * 0.05);
                const gp_Pnt before = hole.axisLoc.Translated(gp_Vec(hole.axisDir).Multiplied(hole.t0 - eps));
                const gp_Pnt after  = hole.axisLoc.Translated(gp_Vec(hole.axisDir).Multiplied(hole.t1 + eps));
                const int outBefore = classifyOut(classifier, before, 1e-4);
                const int outAfter  = classifyOut(classifier, after, 1e-4);
                if (outBefore == 1 && outAfter == 1)      hole.through = 1;
                else if (outBefore == 0 || outAfter == 0) hole.through = 0;
                else                                      hole.through = -1;

                holes.push_back(hole);
            }
        }

        // Coaxial stacks (counterbores, stepped holes): same axis line, touching extents.
        int nextStack = 0;
        for (std::size_t i = 0; i < holes.size(); ++i)
        {
            if (holes[i].stackId != 0)
                continue;
            holes[i].stackId = ++nextStack;
            for (std::size_t j = i + 1; j < holes.size(); ++j)
            {
                if (holes[j].stackId != 0)
                    continue;
                if (holes[i].axisDir.Angle(holes[j].axisDir) > kParallelTolRad)
                    continue;
                const double tol = std::max(1e-3, std::min(holes[i].radius, holes[j].radius) * 0.01);
                if (axisLineDistance(holes[i].axisLoc, holes[i].axisDir, holes[j].axisLoc) > tol)
                    continue;
                const double shift = gp_Vec(holes[i].axisLoc, holes[j].axisLoc).Dot(gp_Vec(holes[i].axisDir));
                const double j0 = holes[j].t0 + shift;
                const double j1 = holes[j].t1 + shift;
                const double gapTol = std::max(0.05, holes[i].radius * 0.1);
                if (j0 <= holes[i].t1 + gapTol && j1 >= holes[i].t0 - gapTol)
                    holes[j].stackId = holes[i].stackId;
            }
        }
        // Counterbore candidate = the largest bore in a stack that also contains a strictly
        // smaller hole. Equal-diameter coaxial pieces (one bore interrupted by a groove) are not.
        for (DetectedHole& a : holes)
        {
            int size = 0;
            bool largest = true;
            bool hasSmaller = false;
            const double radiusTol = std::max(1e-4, a.radius * 0.005);
            for (const DetectedHole& b : holes)
            {
                if (b.stackId != a.stackId)
                    continue;
                ++size;
                if (b.radius > a.radius + radiusTol)
                    largest = false;
                if (b.radius < a.radius - radiusTol)
                    hasSmaller = true;
            }
            a.stackSize = size;
            a.largestInStack = largest && hasSmaller;
        }

        return holes;
    }

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

    struct ThinWallResult
    {
        bool   ok       = false;
        bool   complete = true;   // false when the time budget cut sampling short
        int    samples  = 0;
        double sampledAreaNative = 0.0;       // area of the faces actually sampled
        double minThicknessNative = -1.0;     // -1: no wall thinner than the largest band
        std::vector<double> bandLimitsIn;     // inches
        std::vector<double> bandAreaNative;   // surface area whose local thickness < limit
    };

    // Local wall thickness by ray casting. Points are sampled on every face (UV grid cell
    // centers inside the face boundary); from each, a ray goes into the material (opposite
    // the outward normal) and the distance to the face it exits through is the local
    // thickness there. Each sample carries its face's area / sample count, and areas thinner
    // than each band limit are summed. Both sides of a thin wall count, since both get
    // machined. Rays are only traced as far as the largest band, and the whole pass is
    // time-boxed so very large parts can't stall the request.
    ThinWallResult measureThinWalls(const TopoDS_Shape& shape, double lengthToInches, double totalAreaNative)
    {
        ThinWallResult r;
        r.bandLimitsIn   = {0.02, 0.04, 0.06, 0.08, 0.10, 0.125, 0.1875, 0.25};
        r.bandAreaNative.assign(r.bandLimitsIn.size(), 0.0);
        if (totalAreaNative <= 0.0 || lengthToInches <= 0.0)
            return r;

        Bnd_Box box;
        BRepBndLib::Add(shape, box);
        if (box.IsVoid())
            return r;
        double x0, y0, z0, x1, y1, z1;
        box.Get(x0, y0, z0, x1, y1, z1);
        const double diag = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) + (z1 - z0) * (z1 - z0));
        if (diag <= 0.0)
            return r;

        const double maxRayNative = (r.bandLimitsIn.back() / lengthToInches) * 1.01;
        // Rays from free-form (B-spline) faces can re-hit their own face within tolerance;
        // nothing under 0.002 in is a real machinable wall, so hits closer than that are skipped.
        const double startEps     = std::max(diag * 1e-5, 0.002 / lengthToInches);
        constexpr double kSampleBudget = 8000.0;
        constexpr int    kMaxGridSide  = 20;
        constexpr double kTimeBudgetSec = 40.0;  // Xano waits up to 120 s for the whole request
        const auto started = std::chrono::steady_clock::now();

        IntCurvesFace_ShapeIntersector inter;
        inter.Load(shape, Precision::Confusion());

        for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next())
        {
            const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
            if (elapsed > kTimeBudgetSec)
            {
                r.complete = false;
                break;
            }

            const TopoDS_Face face = TopoDS::Face(exp.Current());
            GProp_GProps fp;
            BRepGProp::SurfaceProperties(face, fp);
            const double area = std::abs(fp.Mass());
            if (area <= 0.0)
                continue;

            double u0, u1, v0, v1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            if (!(u1 > u0) || !(v1 > v0))
                continue;

            const double target = std::max(4.0, kSampleBudget * area / totalAreaNative);
            const int n = std::min(kMaxGridSide, std::max(2, static_cast<int>(std::ceil(std::sqrt(target)))));

            BRepAdaptor_Surface surf(face, Standard_True);
            BRepTopAdaptor_FClass2d inside(face, Precision::PConfusion());
            std::vector<double> thickness;
            thickness.reserve(static_cast<std::size_t>(n) * n);

            for (int i = 0; i < n; ++i)
            {
                for (int j = 0; j < n; ++j)
                {
                    const double u = u0 + (i + 0.5) * (u1 - u0) / n;
                    const double v = v0 + (j + 0.5) * (v1 - v0) / n;
                    if (inside.Perform(gp_Pnt2d(u, v)) != TopAbs_IN)
                        continue;

                    BRepLProp_SLProps props(surf, u, v, 1, Precision::Confusion());
                    if (!props.IsNormalDefined())
                        continue;
                    gp_Dir outward = props.Normal();
                    if (face.Orientation() == TopAbs_REVERSED)
                        outward.Reverse();

                    double t = -1.0; // -1: no exit within the largest band
                    try
                    {
                        inter.PerformNearest(gp_Lin(props.Value(), outward.Reversed()), startEps, maxRayNative);
                        if (inter.IsDone() && inter.NbPnt() > 0)
                            t = inter.WParameter(1);
                    }
                    catch (...)
                    {
                        t = -1.0;
                    }
                    thickness.push_back(t);
                }
            }

            if (thickness.empty())
                continue;
            const double weight = area / static_cast<double>(thickness.size());
            r.sampledAreaNative += area;
            for (double t : thickness)
            {
                ++r.samples;
                if (t <= 0.0)
                    continue;
                if (r.minThicknessNative < 0.0 || t < r.minThicknessNative)
                    r.minThicknessNative = t;
                const double tIn = t * lengthToInches;
                for (std::size_t b = 0; b < r.bandLimitsIn.size(); ++b)
                    if (tIn < r.bandLimitsIn[b])
                        r.bandAreaNative[b] += weight;
            }
        }

        // Time budget hit: scale the sampled faces up to the whole part (reported via coverage).
        if (!r.complete && r.sampledAreaNative > 0.0)
        {
            const double scale = totalAreaNative / r.sampledAreaNative;
            for (double& a : r.bandAreaNative)
                a *= scale;
        }

        r.ok = true;
        return r;
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
        // 0.1 rad (~5.7°) gives ≈63 segments per full circle → <0.1% diameter error.
        // Relative mode applies linearDeflection per-face so small holes on large parts
        // aren't under-sampled by the body-diagonal heuristic.
        constexpr double angularDeflection = 0.1;
        BRepMesh_IncrementalMesh mesher(shape, linearDeflection, Standard_True, angularDeflection, Standard_True);
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
            sourceStepUnit = "mm";

        // Shape coordinates are stored in OCCT's cascade unit (default "mm"), not
        // necessarily the file's declared unit. Use the cascade unit for all metric
        // conversions; keep sourceStepUnit only for the JSON display field.
        std::string cascadeUnit = "mm";
        if (const char* cu = Interface_Static::CVal("xstep.cascade.unit"))
            cascadeUnit = cu;

        double lengthToInches = unitToInches(cascadeUnit);
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

        // Face-class areas for finishing estimates (native units): planar faces, analytic
        // curved faces (cylinder/cone/sphere/torus/revolution/extrusion = "contour"), and
        // free-form faces (B-spline/Bezier/offset/other = "generic").
        double planarAreaNative  = 0.0;
        double contourAreaNative = 0.0;
        double genericAreaNative = 0.0;

        // Cylindrical faces reduced for hole detection
        std::vector<CylPiece> cylPieces;

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
                case GeomAbs_Plane:
                    planarAreaNative += faceArea;
                    break;
                case GeomAbs_Cylinder:
                case GeomAbs_Cone:
                case GeomAbs_Sphere:
                case GeomAbs_Torus:
                case GeomAbs_SurfaceOfRevolution:
                case GeomAbs_SurfaceOfExtrusion:
                    contourAreaNative += faceArea;
                    break;
                default:
                    genericAreaNative += faceArea;
                    break;
            }

            if (surfType == GeomAbs_Cylinder)
            {
                try
                {
                    CylPiece piece;
                    if (makeCylPiece(face, piece))
                        cylPieces.push_back(piece);
                }
                catch (...) { /* a bad face never blocks the metrics */ }
            }

            switch (surfType)
            {
                case GeomAbs_Plane:    ++planarCount; break;
                case GeomAbs_Cylinder:
                {
                    ++cylCount;
                    appendCurvatureSamples(face, lengthToInches, cylCurv);
                    gp_Ax1 axis;
                    if (cylindricalFaceAxis(face, axis))
                    {
                        CylFaceAxisInfo info;
                        info.axis = axis;
                        info.area = faceArea;
                        cylFaceAxes.push_back(info);
                    }
                    break;
                }
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

        std::vector<DetectedHole> holes;
        try
        {
            holes = detectHoles(shape, cylPieces);
        }
        catch (...)
        {
            holes.clear();
        }

        ThinWallResult thinWalls;
        if (solids > 0)
        {
            try
            {
                thinWalls = measureThinWalls(shape, lengthToInches, totalAreaNative);
            }
            catch (...)
            {
                thinWalls = ThinWallResult();
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
        json << "    },\n";
        const double in2 = lengthToInches * lengthToInches;
        json << "    \"face_classes\": {\"planar\": " << planarAreaNative * in2
             << ", \"contour\": " << contourAreaNative * in2
             << ", \"generic\": " << genericAreaNative * in2
             << ", \"unit\": \"in^2\"},\n";
        int throughCount = 0;
        for (const DetectedHole& h : holes)
            if (h.through == 1) ++throughCount;
        json << "    \"hole_summary\": {\"count\": " << holes.size()
             << ", \"through\": " << throughCount
             << ", \"detail\": \"see features.holes (analyze-step)\"},\n";
        // Surface area by local wall thickness (both sides of a wall count). "area" uses the
        // default 0.06 in cutoff; the backend picks the band matching its profile's cutoff.
        if (thinWalls.ok)
        {
            constexpr double kDefaultThinCutoffIn = 0.06;
            double defaultArea = 0.0;
            for (std::size_t b = 0; b < thinWalls.bandLimitsIn.size(); ++b)
                if (std::abs(thinWalls.bandLimitsIn[b] - kDefaultThinCutoffIn) < 1e-9)
                    defaultArea = thinWalls.bandAreaNative[b] * in2;
            json << "    \"thin_walls\": {\"cutoff_in\": " << kDefaultThinCutoffIn
                 << ", \"area\": " << defaultArea
                 << ", \"min_thickness_in\": ";
            if (thinWalls.minThicknessNative > 0.0)
                json << thinWalls.minThicknessNative * lengthToInches;
            else
                json << "null";
            json << ", \"bands\": [";
            for (std::size_t b = 0; b < thinWalls.bandLimitsIn.size(); ++b)
            {
                json << (b ? ", " : "") << "{\"max_thickness_in\": " << thinWalls.bandLimitsIn[b]
                     << ", \"area\": " << thinWalls.bandAreaNative[b] * in2 << "}";
            }
            json << "], \"samples\": " << thinWalls.samples
                 << ", \"complete\": " << (thinWalls.complete ? "true" : "false")
                 << ", \"coverage\": " << (totalAreaNative > 0.0 ? thinWalls.sampledAreaNative / totalAreaNative : 0.0)
                 << ", \"unit\": \"in^2\"}\n";
        }
        else
        {
            json << "    \"thin_walls\": null\n";
        }
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
        json << "  },\n";

        // Full hole list. Top-level (outside "geometry") on purpose: the GLB endpoint copies
        // "geometry" into an HTTP header, and a long hole list must never go there.
        json << "  \"features\": {\n";
        json << "    \"method\": \"B-rep: concave cylindrical faces grouped by radius + coaxial axis + overlapping extent; hole when combined arc >= 300 deg; through = air beyond both ends\",\n";
        json << "    \"unit\": \"in\",\n";
        json << "    \"holes\": [";
        for (std::size_t i = 0; i < holes.size(); ++i)
        {
            const DetectedHole& h = holes[i];
            const double tMid = 0.5 * (h.t0 + h.t1);
            const gp_Pnt c  = h.axisLoc.Translated(gp_Vec(h.axisDir).Multiplied(tMid));
            const gp_Pnt e0 = h.axisLoc.Translated(gp_Vec(h.axisDir).Multiplied(h.t0));
            const gp_Pnt e1 = h.axisLoc.Translated(gp_Vec(h.axisDir).Multiplied(h.t1));
            const double dia   = 2.0 * h.radius * lengthToInches;
            const double depth = (h.t1 - h.t0) * lengthToInches;
            const double cx = c.X() * lengthToInches, cy = c.Y() * lengthToInches, cz = c.Z() * lengthToInches;

            // Stable geometric key: rounded diameter, center and axis. Same STEP -> same keys,
            // so the backend can match re-detections to existing rows and keep user tags.
            // Rounded to fixed decimals with -0 folded to 0, so float noise around zero
            // cannot flip a key between runs.
            const auto stable = [](double v, double scale) -> double {
                double r = std::round(v * scale) / scale;
                return (r == 0.0) ? 0.0 : r;
            };
            std::ostringstream key;
            key << std::fixed << std::setprecision(3) << "d" << stable(dia, 1000.0)
                << std::setprecision(2) << "_c" << stable(cx, 100.0) << "," << stable(cy, 100.0) << "," << stable(cz, 100.0)
                << "_a" << stable(h.axisDir.X(), 100.0) << "," << stable(h.axisDir.Y(), 100.0) << "," << stable(h.axisDir.Z(), 100.0);

            const char* suggested = (h.stackSize > 1 && h.largestInStack) ? "counterbore" : "simple";
            const char* through = h.through == 1 ? "true" : (h.through == 0 ? "false" : "null");

            json << (i ? ",\n" : "\n");
            json << "      {\"key\": \"" << escapeJson(key.str()) << "\", "
                 << "\"diameter\": " << dia << ", "
                 << "\"depth\": " << depth << ", "
                 << "\"through\": " << through << ", "
                 << "\"center\": [" << cx << ", " << cy << ", " << cz << "], "
                 << "\"axis\": [" << h.axisDir.X() << ", " << h.axisDir.Y() << ", " << h.axisDir.Z() << "], "
                 << "\"end_points\": [[" << e0.X() * lengthToInches << ", " << e0.Y() * lengthToInches << ", " << e0.Z() * lengthToInches << "], ["
                 << e1.X() * lengthToInches << ", " << e1.Y() * lengthToInches << ", " << e1.Z() * lengthToInches << "]], "
                 << "\"arc_degrees\": " << h.arcRad * 180.0 / M_PI << ", "
                 << "\"face_count\": " << h.faces << ", "
                 << "\"stack_id\": " << h.stackId << ", "
                 << "\"stack_size\": " << h.stackSize << ", "
                 << "\"suggested_type\": \"" << suggested << "\"}";
        }
        json << (holes.empty() ? "]\n" : "\n    ]\n");
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



