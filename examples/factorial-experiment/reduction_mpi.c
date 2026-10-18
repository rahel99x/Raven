/* Three declared ranks, followed by an explicitly controlled root reduction.
 * This exercises real MPI transport; it does not claim to control MPI_Reduce's
 * internal tree or a vendor's BLAS/GPU implementation. */
#include <mpi.h>
#include <float.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    int rank, size;
    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    if (size != 3 || argc != 3) { MPI_Finalize(); return 2; }
    double local = rank == 0 ? 1e16 : rank == 1 ? 1.0 : -1e16;
    double values[3] = {0,0,0};
    MPI_Gather(&local,1,MPI_DOUBLE,values,1,MPI_DOUBLE,0,MPI_COMM_WORLD);
    if (rank == 0) {
        if (!strcmp(argv[1],"paired")) { double swap=values[1]; values[1]=values[2]; values[2]=swap; }
        double result;
        int supported = 1;
        if (!strcmp(argv[2],"accurate")) {
            long double total=0;
            for(int i=0;i<3;i++) total+=(long double)values[i];
            result=(double)total;
            supported = LDBL_MANT_DIG > DBL_MANT_DIG;
        } else { result=0; for(int i=0;i<3;i++) result+=values[i]; }
        printf("%.17g %d\n",result,supported);
    }
    MPI_Finalize();
    return 0;
}
