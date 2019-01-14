#include <iostream>
#include <ctime> 
using namespace std;

int main()
{
	const int n = 365;
	int a[n] = {};
	int max = -100;
	int j = 0;

	srand(time(NULL));
	for (int i = 0; i < 365; i++)
	{
		a[i] = rand() % 61 - 30;
	
	}
		for (int i = 152; i < 182; i++) {
			if (a[i] > max) {
				max = a[i];
				j = i;

			}
			
		}

		cout<<"the warmest day:" << j << endl;
	
		cout << "7 days before:" << endl;
		int w[7] = {};
		for (int k = 7; k > 0; k--) {
			int i = j - k;
			w[k] = a[i];
			cout << w[k] << endl;
		}
		
	
	system("pause");
	return 0;
}