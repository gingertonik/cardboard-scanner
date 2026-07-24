using CoenM.ImageHash;
using CoenM.ImageHash.HashAlgorithms;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.PixelFormats;
using SixLabors.ImageSharp.Processing;

namespace CardScanner.Services;

/// <summary>
/// Computes and compares 64-bit perceptual hashes of card images.
/// Used both to build the local Scryfall index and to match live-detected cards.
/// </summary>
public sealed class PerceptualHasher
{
    private readonly PerceptualHash _algo = new();

    // Art window as fractions of a full, upright card. Applied identically to indexed
    // Scryfall images and to the live warped card so their art hashes are comparable.
    // The art hash largely ignores the title/type bars and outer border, so it survives
    // foil glare and title-text damage better than the whole-card hash.
    private const double ArtX0 = 0.090, ArtY0 = 0.110, ArtX1 = 0.910, ArtY1 = 0.560;

    public ulong Hash(Image<Rgba32> image) => _algo.Hash(image);

    public ulong HashFromEncoded(byte[] encodedImage)
    {
        using var img = Image.Load<Rgba32>(encodedImage);
        return _algo.Hash(img);
    }

    /// <summary>Compute both the whole-card hash and the art-crop hash from one image.</summary>
    public (ulong Full, ulong Art) HashFullAndArt(byte[] encodedImage)
    {
        using var img = Image.Load<Rgba32>(encodedImage);
        ulong full = _algo.Hash(img);

        int x = (int)(img.Width * ArtX0);
        int y = (int)(img.Height * ArtY0);
        int w = Math.Max(1, (int)(img.Width * (ArtX1 - ArtX0)));
        int h = Math.Max(1, (int)(img.Height * (ArtY1 - ArtY0)));
        // Clamp to bounds.
        w = Math.Min(w, img.Width - x);
        h = Math.Min(h, img.Height - y);

        using var art = img.Clone(ctx => ctx.Crop(new Rectangle(x, y, w, h)));
        ulong artHash = _algo.Hash(art);
        return (full, artHash);
    }

    /// <summary>Hamming distance between two 64-bit hashes (0 = identical, 64 = opposite).</summary>
    public static int HammingDistance(ulong a, ulong b)
        => System.Numerics.BitOperations.PopCount(a ^ b);

    /// <summary>Similarity as a 0..1 fraction of matching bits.</summary>
    public static double Similarity(ulong a, ulong b)
        => (64 - HammingDistance(a, b)) / 64.0;
}
